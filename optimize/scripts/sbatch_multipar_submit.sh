#!/bin/bash
#SBATCH --partition=ampere
#SBATCH --job-name=larndsim-fit
#SBATCH --output=output-%j.txt --error=output-%j.txt
#SBATCH --ntasks-per-node=1
#SBATCH --gpus-per-node=a100:1
#SBATCH --cpus-per-task=1
#SBATCH --time=72:00:00
#SBATCH --array=1,2,3,4,5

seed=$SLURM_ARRAY_TASK_ID
seed_init=$SLURM_ARRAY_TASK_ID
data_seed=${seed}

#INPUT_FILE=/sdf/group/neutrino/cyifan/muon-sim/fake_data_S1/edepsim-output.h5
# INPUT_FILE=/sdf/home/b/bkroul/l-sim/h5/proton_no_nuclei.h5
INPUT_FILE=/sdf/home/b/bkroul/l-sim/h5/proton_max-dEdx2.h5
#INPUT_FILE=/sdf/home/b/bkroul/l-sim/h5/proton_min-dEdx5.h5

SIF_FILE=/sdf/group/neutrino/images/larndsim_latest.sif
PARAM=/sdf/home/b/bkroul/larnd-sim/optimize/scripts/param_list.yaml

max_abs_costheta_sel=0.966; min_abs_segz_sel=15; track_z_bound=28; track_len_sel=2; # dataio values
batch_memory=32768; max_grad_clip=1 

label=proton_max-2_i=dt=seed${seed}__btch${batch_memory}MB

singularity exec -B /sdf --nv --bind /fs ${SIF_FILE} \
  python3 -m optimize.example_run \
    --preload \
    --vary-init \
    --seed-init ${seed_init} \
    --no-noise-guess \
    --seed ${seed} \
    --data_seed ${data_seed} \
    --out_label ${label} \
    --params ${PARAM} \
    --input_file ${INPUT_FILE} \
    --print_input \
    --data_sz -1 \
    --max_nbatch -1 \
    --num_workers 0 \
    --track_len_sel ${track_len_sel} \
    --track_z_bound ${track_z_bound} \
    --max_abs_costheta_sel ${max_abs_costheta_sel} \
    --min_abs_segz_sel ${min_abs_segz_sel} \
    --random_ntrack \
    --max_batch_len 100 \
    --iterations 5000 \
    --optimizer_fn Adam \
    --loss_fn SDTW \
    --link-vdrift-eField \
    --batch_memory ${batch_memory} \
    --skip_pixels \
    --lr_scheduler ExponentialLR \
    --lr_kw '{"gamma" : 0.95 }' \
    --max_clip_norm_val ${max_grad_clip}