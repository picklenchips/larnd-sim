"""
Module that simulates the front-end electronics (triggering, ADC)
"""

import numpy as np
import cupy as cp
import h5py
import yaml

from numba import cuda
from numba.cuda.random import xoroshiro128p_normal_float32
from math import exp

from larpix.packet import Packet_v2, TimestampPacket, TriggerPacket
from larpix.packet import PacketCollection
from larpix.format import hdf5format
from tqdm import tqdm
from .consts import detector, physics

from .pixels_from_track import id2pixel

#: Maximum number of ADC values stored per pixel
MAX_ADC_VALUES = 10
#: Discrimination threshold
DISCRIMINATION_THRESHOLD = 7e3 * physics.E_CHARGE
#: ADC hold delay in clock cycles
ADC_HOLD_DELAY = 15
#: ADC busy delay in clock cycles
ADC_BUSY_DELAY = 8
#: Reset time in clock cycles
RESET_CYCLES = 1
#: Clock cycle time in :math:`\mu s`
CLOCK_CYCLE = 0.1
#: Front-end gain in :math:`mV/ke-`
GAIN = 4/1e3
#: Buffer risetime in :math:`\mu s` (set >0 to include buffer response simulation)
BUFFER_RISETIME = 0 # 0.170
#: Common-mode voltage in :math:`mV`
V_CM = 288
#: Reference voltage in :math:`mV`
V_REF = 1300
#: Pedestal voltage in :math:`mV`
V_PEDESTAL = 580
#: Number of ADC counts
ADC_COUNTS = 2**8
#: Reset noise in e-
RESET_NOISE_CHARGE = 900
#: Uncorrelated noise in e-
UNCORRELATED_NOISE_CHARGE = 500
#: Discriminator noise in e-
DISCRIMINATOR_NOISE = 650
#: Average time between events in clock cycles
EVENT_RATE = 1000000 # ~10Hz

import logging
logging.basicConfig()
logger = logging.getLogger('fee')
logger.setLevel(logging.WARNING)
logger.info("ELECTRONICS SIMULATION")

def rotate_tile(pixel_id, tile_id):
    """
    Returns the pixel ID of the rotated tile.

    Args:
        pixel_id(int): pixel ID
        tile_id(int): tile ID

    Returns:
        tuple: pixel indeces
    """
    axes = detector.TILE_ORIENTATIONS[tile_id]
    x_axis = axes[2]
    y_axis = axes[1]

    pix_x = pixel_id[0]
    if x_axis < 0:
        pix_x = detector.N_PIXELS_PER_TILE[0]-pixel_id[0]-1

    pix_y = pixel_id[1]
    if y_axis < 0:
        pix_y = detector.N_PIXELS_PER_TILE[1]-pixel_id[1]-1

    return pix_x, pix_y

def export_to_hdf5(event_id_list,
                   adc_list,
                   adc_ticks_list,
                   unique_pix,
                   current_fractions,
                   track_ids,
                   filename,
                   bad_channels=None):
    """
    Saves the ADC counts in the LArPix HDF5 format.
    Args:
        event_id_list (:obj:`numpy.ndarray`): list of event ids for each ADC value for each pixel
        adc_list (:obj:`numpy.ndarray`): list of ADC values for each pixel
        adc_ticks_list (:obj:`numpy.ndarray`): list of time ticks for each pixel
        unique_pix (:obj:`numpy.ndarray`): list of pixel IDs
        current_fractions (:obj:`numpy.ndarray`): array containing the fractional current
            induced by each track on each pixel
        track_ids (:obj:`numpy.ndarray`): 2D array containing the track IDs associated
            to each pixel
        filename (str): filename of HDF5 output file
        bad_channels (dict): dictionary containing as value a list of bad channels and as
            the chip key
    Returns:
        tuple: a tuple containing the list of LArPix packets and the list of entries for the `mc_packets_assn` dataset
    """

    dtype = np.dtype([('track_ids','(%i,)i8' % track_ids.shape[1]), ('fraction', '(%i,)f8' % current_fractions.shape[2])])
    packets = [TimestampPacket()]
    packets_mc = [[-1]*track_ids.shape[1]]
    packets_frac = [[0]*current_fractions.shape[2]]
    packets_mc_ds = []
    last_event = -1

    if bad_channels:
        with open(bad_channels, 'r') as bad_channels_file:
            bad_channels_list = yaml.load(bad_channels_file, Loader=yaml.FullLoader)

    unique_events, unique_events_inv = np.unique(event_id_list[...,0], return_inverse=True)
    event_start_time = np.random.exponential(scale=EVENT_RATE, size=unique_events.shape).astype(int)
    event_start_time = np.cumsum(event_start_time)
    event_start_time_list = event_start_time[unique_events_inv]

    for itick, adcs in enumerate(tqdm(adc_list, desc="Writing to HDF5...", ncols=80)):
        ts = adc_ticks_list[itick]
        pixel_id = unique_pix[itick]

        pix_x, pix_y, plane_id = id2pixel(pixel_id)
        module_id = plane_id//2+1
        tile_x = int(pix_x//detector.N_PIXELS_PER_TILE[0])
        tile_y = int(pix_y//detector.N_PIXELS_PER_TILE[1])
        anode_id = 0 if plane_id % 2 == 0 else 1
        tile_id = detector.TILE_MAP[anode_id][tile_x][tile_y]

        for iadc, adc in enumerate(adcs):
            t = ts[iadc]

            if adc > digitize(0):
                event = event_id_list[itick,iadc]
                event_t0 = event_start_time_list[itick]

                if event_t0 > 2**31-1:
                    # 31-bit rollover
                    packets.append(TimestampPacket(timestamp=(2**31) * CLOCK_CYCLE * 1e6))
                    packets_mc.append([-1]*track_ids.shape[1])
                    packets_frac.append([0]*current_fractions.shape[2])
                    event_start_time_list[itick:] -= 2**31

                event_t0 = event_t0 % (2**31)
                time_tick = int(np.floor(t/CLOCK_CYCLE + event_t0)) % (2**31)

                if event != last_event:
                    packets.append(TriggerPacket(io_group=1,trigger_type=b'\x02',timestamp=event_t0))
                    packets_mc.append([-1]*track_ids.shape[1])
                    packets_frac.append([0]*current_fractions.shape[2])
                    packets.append(TriggerPacket(io_group=2,trigger_type=b'\x02',timestamp=event_t0))
                    packets_mc.append([-1]*track_ids.shape[1])
                    packets_frac.append([0]*current_fractions.shape[2])
                    last_event = event

                p = Packet_v2()

                try:
                    chip, channel = detector.PIXEL_CONNECTION_DICT[rotate_tile((pix_x % detector.N_PIXELS_PER_TILE[0],
                                                                                pix_y % detector.N_PIXELS_PER_TILE[1]),
                                                                   tile_id)]
                except KeyError:
                    logger.warning("Pixel ID not valid", pixel_id)
                    continue

                p.dataword = int(adc)
                p.timestamp = time_tick

                try:
                    io_group_io_channel = detector.TILE_CHIP_TO_IO[tile_id][chip]
                except KeyError:
                    logger.info(f"Chip {chip} on tile {tile_id} not found")
                    continue

                io_group, io_channel = io_group_io_channel // 1000, io_group_io_channel % 1000
                io_group = detector.MODULE_TO_IO_GROUPS[module_id][io_group-1]
                chip_key = "%i-%i-%i" % (io_group, io_channel, chip)

                if bad_channels:
                    if chip_key in bad_channels_list:
                        if channel in bad_channels_list[chip_key]:
                            logger.info(f"Channel {channel} on chip {chip_key} disabled")
                            continue

                p.chip_key = chip_key
                p.channel_id = channel
                p.packet_type = 0
                p.first_packet = 1
                p.assign_parity()

                packets_mc.append(track_ids[itick])
                packets_frac.append(current_fractions[itick][iadc])
                packets.append(p)
            else:
                break

    packet_list = PacketCollection(packets, read_id=0, message='')

    hdf5format.to_file(filename, packet_list)

    if packets:
        packets_mc_ds = np.empty(len(packets), dtype=dtype)
        packets_mc_ds['track_ids'] = packets_mc
        packets_mc_ds['fraction'] = packets_frac

    with h5py.File(filename, 'a') as f:
        if "mc_packets_assn" in f.keys():
            del f['mc_packets_assn']
        f.create_dataset("mc_packets_assn", data=packets_mc_ds)

        f['configs'].attrs['vdrift'] = detector.V_DRIFT
        f['configs'].attrs['long_diff'] = detector.LONG_DIFF
        f['configs'].attrs['tran_diff'] = detector.TRAN_DIFF
        f['configs'].attrs['lifetime'] = detector.ELECTRON_LIFETIME
        f['configs'].attrs['drift_length'] = detector.DRIFT_LENGTH

    return packets, packets_mc_ds


def digitize(integral_list):
    """
    The function takes as input the integrated charge and returns the digitized
    ADC counts.

    Args:
        integral_list(:obj:`numpy.ndarray`): list of charge collected by each pixel

    Returns:
        :obj:`numpy.ndarray`: list of ADC values for each pixel
    """
    xp = cp.get_array_module(integral_list)
    adcs = xp.minimum(xp.around(xp.maximum((integral_list * GAIN / physics.E_CHARGE + V_PEDESTAL - V_CM), 0)
                                * ADC_COUNTS / (V_REF - V_CM)), ADC_COUNTS)

    return adcs

@cuda.jit
def get_adc_values(pixels_signals,
                   pixels_signals_tracks,
                   time_ticks,
                   adc_list,
                   adc_ticks_list,
                   time_padding,
                   rng_states,
                   current_fractions,
                   pixel_thresholds):
    """
    Implementation of self-trigger logic

    Args:
        pixels_signals (:obj:`numpy.ndarray`): list of induced currents for
            each pixel
        pixels_signals_tracks (:obj:`numpy.ndarray`): list of induced currents
            for each track that induces current on each pixel
        time_ticks (:obj:`numpy.ndarray`): list of time ticks for each pixel
        adc_list (:obj:`numpy.ndarray`): list of integrated charges for each
            pixel
        adc_ticks_list (:obj:`numpy.ndarray`): list of the time ticks that
            correspond to each integrated charge
        time_padding (float): time interval to add to each time tick.
        rng_states (:obj:`numpy.ndarray`): array of random states for noise
            generation
        current_fractions (:obj:`numpy.ndarray`): 2D array that will contain
            the fraction of current induced on the pixel by each track
        pixel_thresholds(: obj: `numpy.ndarray`): list of discriminator
            thresholds for each pixel
    """
    ip = cuda.grid(1)

    if ip < pixels_signals.shape[0]:
        curre = pixels_signals[ip]
        ic = 0
        iadc = 0
        adc_busy = 0
        last_reset = 0
        q_sum = xoroshiro128p_normal_float32(rng_states, ip) * RESET_NOISE_CHARGE * physics.E_CHARGE

        while ic < curre.shape[0] or adc_busy > 0:

            if iadc >= MAX_ADC_VALUES:
                print("More ADC values than possible,", MAX_ADC_VALUES)
                break

            q = 0
            if ic < curre.shape[0]:
                if BUFFER_RISETIME > 0:
                    for jc in range(last_reset, min(ic+1, curre.shape[0])):
                        w = exp((jc - ic) * detector.TIME_SAMPLING / BUFFER_RISETIME) * (1 - exp(-detector.TIME_SAMPLING/BUFFER_RISETIME))
                        q += curre[jc] * detector.TIME_SAMPLING * w

                        for itrk in range(current_fractions.shape[2]):
                            current_fractions[ip][iadc][itrk] += pixels_signals_tracks[ip][ic][itrk] * detector.TIME_SAMPLING * w

                else:
                    q += curre[ic] * detector.TIME_SAMPLING
                    for itrk in range(current_fractions.shape[2]):
                        current_fractions[ip][iadc][itrk] += pixels_signals_tracks[ip][ic][itrk] * detector.TIME_SAMPLING

            q_sum += q

            q_noise = xoroshiro128p_normal_float32(rng_states, ip) * UNCORRELATED_NOISE_CHARGE * physics.E_CHARGE
            disc_noise = xoroshiro128p_normal_float32(rng_states, ip) * DISCRIMINATOR_NOISE * physics.E_CHARGE

            if adc_busy > 0:
                adc_busy -= 1

            if q_sum + q_noise >= pixel_thresholds[ip] + disc_noise and adc_busy == 0:
                crossing_time_tick = ic

                interval = round((3 * CLOCK_CYCLE + ADC_HOLD_DELAY * CLOCK_CYCLE) / detector.TIME_SAMPLING)
                integrate_end = ic+interval

                ic+=1

                while ic <= integrate_end and ic < curre.shape[0]:
                    q = 0

                    if BUFFER_RISETIME > 0:
                        for jc in range(last_reset, min(ic+1, curre.shape[0])):
                            w = exp((jc - ic) * detector.TIME_SAMPLING / BUFFER_RISETIME) * (1 - exp(-detector.TIME_SAMPLING/BUFFER_RISETIME))
                            q += curre[jc] * detector.TIME_SAMPLING * w

                            for itrk in range(current_fractions.shape[2]):
                                current_fractions[ip][iadc][itrk] += pixels_signals_tracks[ip][ic][itrk] * detector.TIME_SAMPLING * w

                    else:
                        q += curre[ic] * detector.TIME_SAMPLING
                        for itrk in range(current_fractions.shape[2]):
                            current_fractions[ip][iadc][itrk] += pixels_signals_tracks[ip][ic][itrk] * detector.TIME_SAMPLING
                            
                    q_sum += q
                    ic+=1

                adc = q_sum + xoroshiro128p_normal_float32(rng_states, ip) * UNCORRELATED_NOISE_CHARGE * physics.E_CHARGE
                disc_noise = xoroshiro128p_normal_float32(rng_states, ip) * DISCRIMINATOR_NOISE * physics.E_CHARGE

                if adc < pixel_thresholds[ip] + disc_noise:
                    ic += round(CLOCK_CYCLE / detector.TIME_SAMPLING)
                    q_sum = xoroshiro128p_normal_float32(rng_states, ip) * UNCORRELATED_NOISE_CHARGE * physics.E_CHARGE

                    for itrk in range(current_fractions.shape[2]):
                        current_fractions[ip][iadc][itrk] = 0
                    last_reset = ic
                    continue

                tot_backtracked = 0
                for itrk in range(current_fractions.shape[2]):
                    tot_backtracked += current_fractions[ip][iadc][itrk]

                for itrk in range(current_fractions.shape[2]):
                    current_fractions[ip][iadc][itrk] /= tot_backtracked

                adc_list[ip][iadc] = adc

                #+2-tick delay from when the PACMAN receives the trigger and when it registers it.
                adc_ticks_list[ip][iadc] = time_ticks[crossing_time_tick]+time_padding+2

                ic += round(RESET_CYCLES * CLOCK_CYCLE / detector.TIME_SAMPLING)
                last_reset = ic
                adc_busy = round(ADC_BUSY_DELAY * CLOCK_CYCLE / detector.TIME_SAMPLING)
                
                q_sum = xoroshiro128p_normal_float32(rng_states, ip) * RESET_NOISE_CHARGE * physics.E_CHARGE

                iadc += 1
                continue

            ic += 1
