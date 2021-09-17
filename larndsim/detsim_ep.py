"""
Module that calculates the current induced by edep-sim track segments
on the pixels
"""
import eagerpy as ep
import torch
import numpy as np
from math import pi, ceil, sqrt, erf, exp, log, floor

from .consts import pixel_pitch, tpc_borders, time_interval, n_pixels
from . import consts
from . import fee

import logging

logging.basicConfig()
logger = logging.getLogger('detsim')
logger.setLevel(logging.WARNING)
logger.info("DETSIM MODULE PARAMETERS")


def time_intervals(event_id_map, tracks, fields):
    """
    Find the value of the longest signal time and stores the start
    time of each segment.

    Args:
        event_id_map (:obj:`numpy.ndarray`, `pyTorch/Tensorflow/JAX Tensor`): array containing
            the event ID corresponding to each track
        tracks (:obj:`numpy.ndarray`, `pyTorch/Tensorflow/JAX Tensor`): array containing the segment
            information
        fields (list): an ordered string list of field/column name of the tracks structured array
    Returns:
        track_starts (:obj:`numpy.ndarray`, `pyTorch/Tensorflow/JAX Tensor`): array where
            we store the segments start time
        time_max (:obj:`numpy.ndarray`, `pyTorch/Tensorflow/JAX Tensor`): array where we store
            the longest signal time
    """
    event_id_map_ep = ep.astensor(event_id_map)
    tracks_ep = ep.astensor(tracks)
    tracks_t_end = tracks_ep[:, fields.index("t_end")]
    tracks_t_start = tracks_ep[:, fields.index("t_start")]
    t_end = ep.minimum(ep.full_like(tracks_t_end, time_interval[1]),
                       ((tracks_t_end + consts.time_padding + 0.5 / consts.vdrift) / consts.t_sampling) * consts.t_sampling)
    t_start = ep.maximum(ep.full_like(tracks_t_start, time_interval[0]),
                         ((tracks_t_start - consts.time_padding) / consts.t_sampling) * consts.t_sampling)
    t_length = t_end - t_start
    track_starts = (t_start + event_id_map_ep * time_interval[1] * 3).raw
    time_max = (ep.max((t_length / consts.t_sampling).astype(int)+1)).raw
    return track_starts, time_max


def z_interval(start_point, end_point, x_p, y_p, tolerance, eps=1e-12):
    """
    Here we calculate the interval in the drift direction for the pixel pID
    using the impact factor

    Args:
        start_point (tuple): coordinates of the segment start
        end_point (tuple): coordinates of the segment end
        x_p (float): pixel center `x` coordinate
        y_p (float): pixel center `y` coordinate
        tolerance (float): maximum distance between the pixel center and
            the segment

    Returns:
        tuple: `z` coordinate of the point of closest approach (POCA),
        `z` coordinate of the first slice, `z` coordinate of the last slice.
        (0,0,0) if POCA > tolerance.
    """
    cond = start_point[:, 0] < end_point[:, 0]
    start = ep.where(cond[..., ep.newaxis], start_point, end_point)
    end = ep.where(cond[..., ep.newaxis], end_point, start_point)

    xs, ys = start[:, 0], start[:, 1]
    xe, ye = end[:, 0], end[:, 1]

    m = (ye - ys) / (xe - xs + eps)
    q = (xe * ys - xs * ye) / (xe - xs + eps)

    a, b, c = m[...,ep.newaxis], -1, q[...,ep.newaxis]

    x_poca = (b * (b * x_p[...,0] - a * y_p[...,0]) - a * c) / (a * a + b * b)
    doca = ep.abs(a * x_p[...,0] + b * y_p[...,0] + c) / ep.sqrt(a * a  + b * b)

    vec3D = end - start
    length3D = ep.norms.l2(vec3D, axis=1, keepdims=True)
    dir3D = vec3D / length3D

    #TODO: Fixme. Not efficient. Should just flip start and end
    end = end[...,ep.newaxis]
    start = start[..., ep.newaxis]
    cond2 = x_poca > end[:, 0]
    cond1 = x_poca < start[:, 0]
    doca = ep.where(cond2,
                    ep.sqrt((x_p[...,0] - end[:, 0]) ** 2 + (y_p[...,0] - end[:, 1]) ** 2),
                    doca)
    doca = ep.where(cond1,
                    ep.sqrt((x_p[...,0] - start[:, 0]) ** 2 + (y_p[...,0] - start[:, 1]) ** 2),
                    doca)

    x_poca = ep.where(cond2, end[:, 0], x_poca)
    x_poca = ep.where(cond1, start[:, 0], x_poca)
    z_poca = start[:, 2] + (x_poca - start[:, 0]) / dir3D[:, 0][..., ep.newaxis] * dir3D[:, 2][..., ep.newaxis]

    length2D = ep.norms.l2(vec3D[...,:2], axis=1, keepdims=True)
    dir2D = vec3D[...,:2] / length2D
    deltaL2D = ep.sqrt(tolerance[..., ep.newaxis] ** 2 - doca ** 2)  # length along the track in 2D

    x_plusDeltaL = x_poca + deltaL2D * dir2D[:,0][..., ep.newaxis]  # x coordinates of the tolerance range
    x_minusDeltaL = x_poca - deltaL2D * dir2D[:,0][..., ep.newaxis]
    plusDeltaL = (x_plusDeltaL - start[:,0,:]) / dir3D[:,0][..., ep.newaxis]  # length along the track in 3D
    minusDeltaL = (x_minusDeltaL - start[:,0,:]) / dir3D[:,0][..., ep.newaxis]  # of the tolerance range

    plusDeltaZ = start[:,2,:] + dir3D[:,2][..., ep.newaxis] * plusDeltaL  # z coordinates of the
    minusDeltaZ = start[:,2,:] + dir3D[:,2][..., ep.newaxis] * minusDeltaL  # tolerance range

    cond = tolerance[..., ep.newaxis] > doca
    z_poca = ep.where(cond, z_poca, 0)
    z_min_delta = ep.where(cond, ep.minimum(minusDeltaZ, plusDeltaZ), 0)
    z_max_delta = ep.where(cond, ep.maximum(minusDeltaZ, plusDeltaZ), 0)
    return z_poca, z_min_delta, z_max_delta

def _b(x, y, z, start, sigmas, segment, Deltar):
    return -((x - start[:, ep.newaxis, 0, ep.newaxis, ep.newaxis, ep.newaxis]) / (sigmas[:, ep.newaxis, 0, ep.newaxis, ep.newaxis, ep.newaxis] * sigmas[:, ep.newaxis, 0, ep.newaxis, ep.newaxis, ep.newaxis]) * (segment[:, ep.newaxis, 0, ep.newaxis, ep.newaxis, ep.newaxis] / Deltar[:, ep.newaxis, ep.newaxis, ep.newaxis, ep.newaxis]) + \
             (y - start[:, ep.newaxis, ep.newaxis, 1,  ep.newaxis, ep.newaxis]) / (sigmas[:, ep.newaxis, ep.newaxis, 1,  ep.newaxis, ep.newaxis] * sigmas[:, ep.newaxis, ep.newaxis, 1,  ep.newaxis, ep.newaxis]) * (segment[:, ep.newaxis, ep.newaxis, 1,  ep.newaxis, ep.newaxis] / Deltar[:, ep.newaxis, ep.newaxis, ep.newaxis, ep.newaxis]) + \
             (z - start[:, ep.newaxis, ep.newaxis, ep.newaxis, 2, ep.newaxis]) / (sigmas[:, ep.newaxis, ep.newaxis, ep.newaxis, 2, ep.newaxis] * sigmas[:, ep.newaxis, ep.newaxis, ep.newaxis, 2, ep.newaxis]) * (segment[:, ep.newaxis, ep.newaxis, ep.newaxis, 2, ep.newaxis] / Deltar[:, ep.newaxis, ep.newaxis, ep.newaxis, ep.newaxis]))


def erf_hack(input):
    return ep.astensor(torch.erf(input.raw))

def rho(point, q, start, sigmas, segment):
    """
    Function that returns the amount of charge at a certain point in space

    Args:
        point (tuple): point coordinates
        q (float): total charge
        start (tuple): segment start coordinates
        sigmas (tuple): diffusion coefficients
        segment (tuple): segment sizes

    Returns:
        float: the amount of charge at `point`.
    """
    x, y, z = point
    Deltax, Deltay, Deltaz = segment[..., 0], segment[..., 1], segment[..., 2]
    Deltar = ep.sqrt(Deltax**2+Deltay**2+Deltaz**2)
    a = ((Deltax/Deltar) * (Deltax/Deltar) / (2*sigmas[:, 0]*sigmas[:, 0]) + \
         (Deltay/Deltar) * (Deltay/Deltar) / (2*sigmas[:, 1]*sigmas[:, 1]) + \
         (Deltaz/Deltar) * (Deltaz/Deltar) / (2*sigmas[:, 2]*sigmas[:, 2]))
    factor = q/Deltar/(sigmas[:, 0]*sigmas[:, 1]*sigmas[:, 2]*sqrt(8*pi*pi*pi))
    sqrt_a_2 = 2*ep.sqrt(a)

    b = _b(x, y, z, start, sigmas, segment, Deltar)

    delta = (x-start[:, ep.newaxis, 0, ep.newaxis, ep.newaxis, ep.newaxis])*(x-start[:, ep.newaxis, 0, ep.newaxis, ep.newaxis, ep.newaxis])/(2*sigmas[:, ep.newaxis, 0, ep.newaxis, ep.newaxis, ep.newaxis]*sigmas[:, ep.newaxis, 0, ep.newaxis, ep.newaxis, ep.newaxis]) + \
            (y-start[:, ep.newaxis, ep.newaxis, 1,  ep.newaxis, ep.newaxis])*(y-start[:, ep.newaxis, ep.newaxis, 1,  ep.newaxis, ep.newaxis])/(2*sigmas[:, ep.newaxis, ep.newaxis, 1,  ep.newaxis, ep.newaxis]*sigmas[:, ep.newaxis, ep.newaxis, 1,  ep.newaxis, ep.newaxis]) + \
            (z-start[:, ep.newaxis, ep.newaxis, ep.newaxis, 2, ep.newaxis])*(z-start[:, ep.newaxis, ep.newaxis, ep.newaxis, 2, ep.newaxis])/(2*sigmas[:, ep.newaxis, ep.newaxis, ep.newaxis, 2, ep.newaxis]*sigmas[:, ep.newaxis, ep.newaxis, ep.newaxis, 2, ep.newaxis])


    integral = sqrt(pi) * \
               (-erf_hack(b/sqrt_a_2[:, ep.newaxis, ep.newaxis, ep.newaxis, ep.newaxis]) + 
                erf_hack((b + 2*(a[:, ep.newaxis, ep.newaxis, ep.newaxis, ep.newaxis]*Deltar[:, ep.newaxis, ep.newaxis, ep.newaxis, ep.newaxis]))/sqrt_a_2[:, ep.newaxis, ep.newaxis, ep.newaxis, ep.newaxis])) / \
               sqrt_a_2[:, ep.newaxis, ep.newaxis, ep.newaxis, ep.newaxis]

   # if factor and integral:
    expo = ep.exp(b*b/(4*a[:, ep.newaxis, ep.newaxis, ep.newaxis, ep.newaxis]) - delta + ep.log(factor[:, ep.newaxis, ep.newaxis, ep.newaxis, ep.newaxis]) + ep.log(integral))
    expo = ep.where(expo.isnan(), 0, expo)

    return expo



def truncexpon(x, loc=0, scale=1):
    """
    A truncated exponential distribution.
    To shift and/or scale the distribution use the `loc` and `scale` parameters.
    """
    y = (x - loc) / scale
    
    return ep.where(y>0, ep.exp(-y) / scale, 0)


def current_model(t, t0, x, y):
    """
    Parametrization of the induced current on the pixel, which depends
    on the of arrival at the anode (:math:`t_0`) and on the position
    on the pixel pad.

    Args:
        t (float): time where we evaluate the current
        t0 (float): time of arrival at the anode
        x (float): distance between the point on the pixel and the pixel center
            on the :math:`x` axis
        y (float): distance between the point on the pixel and the pixel center
            on the :math:`y` axis

    Returns:
        float: the induced current at time :math:`t`
    """
    B_params = (1.060, -0.909, -0.909, 5.856, 0.207, 0.207)
    C_params = (0.679, -1.083, -1.083, 8.772, -5.521, -5.521)
    D_params = (2.644, -9.174, -9.174, 13.483, 45.887, 45.887)
    t0_params = (2.948, -2.705, -2.705, 4.825, 20.814, 20.814)

    a = B_params[0] + B_params[1] * x + B_params[2] * y + B_params[3] * x * y + B_params[4] * x * x + B_params[
        5] * y * y
    b = C_params[0] + C_params[1] * x + C_params[2] * y + C_params[3] * x * y + C_params[4] * x * x + C_params[
        5] * y * y
    c = D_params[0] + D_params[1] * x + D_params[2] * y + D_params[3] * x * y + D_params[4] * x * x + D_params[
        5] * y * y
    shifted_t0 = t0 + t0_params[0] + t0_params[1] * x + t0_params[2] * y + \
                 t0_params[3] * x * y + t0_params[4] * x * x + t0_params[5] * y * y

    a = ep.minimum(a, 1)

    return a * truncexpon(-t, -shifted_t0, b) + (1 - a) * truncexpon(-t, -shifted_t0, c)


def track_point(start, direction, z):
    """
    This function returns the segment coordinates for a point along the `z` coordinate

    Args:
        start (tuple): start coordinates
        direction (tuple): direction coordinates
        z (float): `z` coordinate corresponding to the `x`, `y` coordinates

    Returns:
        tuple: the (x,y) pair of coordinates for the segment at `z`
    """
    l = (z - start[:, 2][...,ep.newaxis]) / direction[:, 2][...,ep.newaxis]
    xl = start[:, 0][...,ep.newaxis] + l * direction[:, 0][...,ep.newaxis]
    yl = start[:, 1][...,ep.newaxis] + l * direction[:, 1][...,ep.newaxis]

    return xl, yl

#
def get_pixel_coordinates(pixels):
    """
    Returns the coordinates of the pixel center given the pixel IDs
    """
    tpc_borders_ep = ep.from_numpy(pixels, tpc_borders).float32()
    plane_id = pixels[..., 0] // n_pixels[0]
    borders = ep.stack([tpc_borders_ep[x.astype(int)] for x in plane_id])

    pix_x = (pixels[..., 0] - n_pixels[0] * plane_id) * pixel_pitch + borders[..., 0, 0]
    pix_y = pixels[..., 1] * pixel_pitch + borders[..., 1, 0]
    return pix_x[...,ep.newaxis], pix_y[...,ep.newaxis]


def tracks_current(pixels, tracks, time_max, fields):
    """
    This function calculates the charge induced on the pixels by the input tracks.

    Args:
        pixels (:obj:`numpy.ndarray`, `pyTorch/Tensorflow/JAX Tensor`): 3D array with dimensions S x P x 2, where S is
            the number of track segments, P is the number of pixels and the third dimension
            contains the two pixel ID numbers.
        tracks (:obj:`numpy.ndarray`, `pyTorch/Tensorflow/JAX Tensor`): 2D array containing the detector segments.
        time_max (int) : total number of time ticks (see time_intervals) 
        fields (list): an ordered string list of field/column name of the tracks structured array
    Returns:
        signals (:obj:`numpy.ndarray`, `pyTorch/Tensorflow/JAX Tensor`): 3D array with dimensions S x P x T,
            where S is the number of track segments, P is the number of pixels, and T is
            the number of time ticks.
    """
    pixels = ep.astensor(pixels)
    tracks_ep = ep.astensor(tracks)
    it = ep.arange(pixels, 0, time_max)

    # Pixel coordinates
    x_p, y_p = get_pixel_coordinates(pixels)
    x_p += pixel_pitch / 2
    y_p += pixel_pitch / 2

    start_coords = ep.stack([tracks_ep[:, fields.index("x_start")],
                             tracks_ep[:, fields.index("y_start")],
                             tracks_ep[:, fields.index("z_start")]], axis=1)
    end_coords = ep.stack([tracks_ep[:, fields.index("x_end")],
                           tracks_ep[:, fields.index("y_end")],
                           tracks_ep[:, fields.index("z_end")]], axis=1)
    cond = tracks_ep[:, fields.index("z_start")] < tracks_ep[:, fields.index("z_end")]
    start = ep.where(cond[...,ep.newaxis], start_coords, end_coords)
    end = ep.where(cond[...,ep.newaxis], end_coords, start_coords)
    segment = end - start
    length = ep.norms.l2(end, axis=1, keepdims=True)

    direction = segment / length
    sigmas = ep.stack([tracks_ep[:, fields.index("tran_diff")],
                       tracks_ep[:, fields.index("tran_diff")],
                       tracks_ep[:, fields.index("long_diff")]], axis=1)

    # The impact factor is the the size of the transverse diffusion or, if too small,
    # half the diagonal of the pixel pad
    impact_factor = ep.maximum(ep.sqrt((5 * sigmas[:, 0]) ** 2 + (5 * sigmas[:, 1]) ** 2),
                               ep.full_like(sigmas[:, 0], sqrt(pixel_pitch ** 2 + pixel_pitch ** 2) / 2)) * 2
    z_poca, z_start, z_end = z_interval(start, end, x_p, y_p, impact_factor)
    
    z_start_int = z_start - 4 * sigmas[:, 2][...,ep.newaxis]
    z_end_int = z_end + 4 * sigmas[:, 2][...,ep.newaxis]

    x_start, y_start = track_point(start, direction, z_start)
    x_end, y_end = track_point(start, direction, z_end)

    y_step = (ep.abs(y_end - y_start) + 8 * sigmas[:, 1][...,ep.newaxis]) / (consts.sampled_points - 1)
    x_step = (ep.abs(x_end - x_start) + 8 * sigmas[:, 0][...,ep.newaxis]) / (consts.sampled_points - 1)

    z_sampling = consts.t_sampling / 2.
    z_steps = ep.maximum(consts.sampled_points, ((ep.abs(z_end_int - z_start_int) / z_sampling)+1).astype(int))

    z_step = (z_end_int - z_start_int) / (z_steps - 1)
    t_start = ep.maximum(time_interval[0],
                         (tracks_ep[:, fields.index("t_start")] - consts.time_padding)
                         // consts.t_sampling * consts.t_sampling)
    total_current = 0
    total_charge = 0

    time_tick = t_start[:, ep.newaxis] + it * consts.t_sampling
    iz = ep.arange(z_steps, 0, z_steps.max().item())
    z =  z_start_int[...,ep.newaxis] + iz[ep.newaxis, ep.newaxis, :] * z_step[...,ep.newaxis]
    tpc_borders_ep = ep.from_numpy(pixels, tpc_borders).float32()
    borders = ep.stack([tpc_borders_ep[x.astype(int)] for x in tracks_ep[:, fields.index("pixel_plane")]])
    t0 = (ep.abs(z - borders[..., 2, 0, ep.newaxis, ep.newaxis]) - 0.5) / consts.vdrift

    # FIXME: this sampling is far from ideal, we should sample around the track
    # and not in a cube containing the track
    ix = ep.arange(iz, 0, consts.sampled_points)
    x = x_start[...,ep.newaxis] + \
        ep.sign(direction[..., 0, ep.newaxis, ep.newaxis]) *\
        (ix[ep.newaxis, ep.newaxis, :] * x_step[...,ep.newaxis]  - 4 * sigmas[..., 0, ep.newaxis, ep.newaxis])

    x_dist = ep.abs(x_p - x)

    iy = ep.arange(iz, 0, consts.sampled_points)

    y = y_start[...,ep.newaxis] + \
        ep.sign(direction[..., 1, ep.newaxis, ep.newaxis]) *\
        (iy[ep.newaxis, ep.newaxis, :] * y_step[...,ep.newaxis] - 4 * sigmas[..., 1, ep.newaxis, ep.newaxis])
    y_dist = ep.abs(y_p - y)

    charge =  rho((x[:,:, :, ep.newaxis, ep.newaxis], y[:,:, ep.newaxis, :, ep.newaxis], z[:,:, ep.newaxis, ep.newaxis, :]), tracks_ep[:, fields.index("n_electrons")], start, sigmas, segment)\
     * ep.abs(x_step[..., ep.newaxis, ep.newaxis, ep.newaxis]) * ep.abs(y_step[..., ep.newaxis, ep.newaxis, ep.newaxis]) * ep.abs(z_step[..., ep.newaxis, ep.newaxis, ep.newaxis])


    current = current_model(time_tick[:, ep.newaxis, :, ep.newaxis, ep.newaxis, ep.newaxis], 
                            t0[:, :, ep.newaxis, ep.newaxis, ep.newaxis, :], 
                            x_dist[:, :, ep.newaxis, :, ep.newaxis, ep.newaxis], 
                            y_dist[:, :, ep.newaxis, ep.newaxis, :, ep.newaxis]) * charge[:, :, ep.newaxis, ...] * consts.e_charge

    #Remove terms from sum failing pixel_pitch condition
    current = ep.where(x_dist[:, :, ep.newaxis, :, ep.newaxis, ep.newaxis]>pixel_pitch/2, 0, current)
    current = ep.where(y_dist[:, :, ep.newaxis, ep.newaxis, :, ep.newaxis]> pixel_pitch/2, 0, current)

    #Sum over x, y, z sampling cube
    total_current = current.sum(axis=(3,4,5))

    #0 signal if z_poca == 0
    signals = ep.where(z_poca[:,:, ep.newaxis] != 0, total_current, 0)
   
    return signals


#def sign(x):
#    """
#    Sign function
#    """
#    return 1 if x >= 0 else -1
#
#
# def sum_pixel_signals(pixels_signals, signals, track_starts, index_map):
#     """
#     This function sums the induced current signals on the same pixel.
#
#     Args:
#         pixels_signals (:obj:`numpy.ndarray`): 2D array that will contain the
#             summed signal for each pixel. First dimension is the pixel ID, second
#             dimension is the time tick
#         signals (:obj:`numpy.ndarray`): 3D array with dimensions S x P x T,
#             where S is the number of track segments, P is the number of pixels, and T is
#             the number of time ticks.
#         track_starts (:obj:`numpy.ndarray`): 1D array containing the starting time of
#             each track
#         index_map (:obj:`numpy.ndarray`): 2D array containing the correspondence between
#             the track index and the pixel ID index.
#     """
#     it, ipix, itick = cuda.grid(3)
#
#     if it < signals.shape[0] and ipix < signals.shape[1]:
#
#         index = index_map[it][ipix]
#         start_tick = round(track_starts[it] / consts.t_sampling)
#
#         if itick < signals.shape[2] and index >= 0:
#             itime = start_tick + itick
#             cuda.atomic.add(pixels_signals, (index, itime), signals[it][ipix][itick])
#
#
# def backtrack_adcs(tracks, adc_list, adc_times_list, track_pixel_map, event_id_map, unique_evids, backtracked_id,
#                    shift):
#     pedestal = floor((fee.V_PEDESTAL - fee.V_CM) * fee.ADC_COUNTS / (fee.V_REF - fee.V_CM))
#
#     ip = cuda.grid(1)
#
#     if ip < adc_list.shape[0]:
#         for itrk in range(track_pixel_map.shape[1]):
#             track_index = track_pixel_map[ip][itrk]
#             if track_index >= 0:
#                 track_start_t = tracks["t_start"][track_index]
#                 track_end_t = tracks["t_end"][track_index]
#                 evid = unique_evids[event_id_map[track_index]]
#                 for iadc in range(adc_list[ip].shape[0]):
#
#                     if adc_list[ip][iadc] > pedestal:
#                         adc_time = adc_times_list[ip][iadc]
#                         evid_time = adc_time // (time_interval[1] * 3)
#
#                         if track_start_t - consts.time_padding < adc_time - evid_time * time_interval[
#                             1] * 3 < track_end_t + consts.time_padding + 0.5 / consts.vdrift:
#                             counter = 0
#
#                             while counter < backtracked_id.shape[2] and backtracked_id[ip, iadc, counter] != -1:
#                                 counter += 1
#
#                             if counter < backtracked_id.shape[2]:
#                                 backtracked_id[ip, iadc, counter] = track_index + shift
#
#
# def get_track_pixel_map(track_pixel_map, unique_pix, pixels):
#     # index of unique_pix array
#     index = cuda.grid(1)
#
#     upix = unique_pix[index]
#
#     for itr in range(pixels.shape[0]):
#         for ipix in range(pixels.shape[1]):
#             pID = pixels[itr][ipix]
#             if upix[0] == pID[0] and upix[1] == pID[1]:
#                 imap = 0
#                 while imap < track_pixel_map.shape[1] and track_pixel_map[index][imap] != -1:
#                     imap += 1
#                 if imap < track_pixel_map.shape[1]:
#                     track_pixel_map[index][imap] = itr
