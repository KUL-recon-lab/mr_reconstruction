"""demo script to show how to solve ADMM subproblem (1) using sigpy"""

import numpy as np
import sigpy
import matplotlib.pyplot as plt
from scipy.ndimage import gaussian_filter

from utils_moco import golden_angle_2d_readout, stacked_nufft_operator

np.random.seed(1)

# oversampled image shape for data simulation
sim_img_shape = (32, 32, 32)
# image shape for reconstruction
recon_img_shape = (8, 16, 16)
# number of spokes and points per spoke
num_spokes = 16
num_points = 32
# transaxial FOV in cm
trans_fov_cm = 40.

# weight of the quadratic penalty term
# if you set this to 0, the result of the recon should be close
# to the ground truth images
lam = 1e1

#-----------------------------------------------------
# max k value according to Nyquist for the recon image shape
kmax_1_cm = 1. / (2 * (trans_fov_cm / recon_img_shape[1]))

# generate 3 test images on the fine (simulation) grid
# for faster execution on a GPU change this to a cupy array

img1 = np.pad(
    np.ones(np.array(sim_img_shape) // 2, dtype=np.complex128),
    ((sim_img_shape[0] // 4, sim_img_shape[0] // 4),
     (sim_img_shape[1] // 4, sim_img_shape[1] // 4),
     (sim_img_shape[2] // 4, sim_img_shape[2] // 4)),
)

img2 = np.roll(img1, 10, axis=1)
img3 = np.roll(img1, -5, axis=2)
img4 = np.roll(img1, 2, axis=2)

# remove high frequencies from ground truth images
img1 = gaussian_filter(img1, 2)
img2 = gaussian_filter(img2, 2)
img3 = gaussian_filter(img3, 2)
img4 = gaussian_filter(img4, 2)

# stack all 3D images into a 4D array
img_4d = np.array([img1, img2, img3, img4])

# setup a 2D coordinates for the NUFFTs
# sigpy needs the coordinates without units
# ranging from -N/2 ... N/2 if we are at Nyquist
# if the k-space coordinates have physical units (1/cm)
# we have to multiply the the FOV (in cm)
kspace_coords_2d = golden_angle_2d_readout(kmax_1_cm * trans_fov_cm,
                                           num_spokes, num_points)

# setup the operators for reconstruction (on a coarser grid)
# setup the operator that acts on a single 3D image
sim_op_3d = stacked_nufft_operator(sim_img_shape,
                                   kspace_coords_2d.reshape(-1, 2))

# setup the operator that applies the 3d operator to a stack of 3 images
rs_in = sigpy.linop.Reshape(sim_img_shape, (1, ) + sim_img_shape)
rs_out = sigpy.linop.Reshape((1, ) + tuple(sim_op_3d.oshape), sim_op_3d.oshape)
ops = img_4d.shape[0] * [rs_out * sim_op_3d * rs_in]
sim_op_4d = sigpy.linop.Diag(ops, iaxis=0, oaxis=0)

# generate (noiseless) data based on the high-res ground truth images
data_4d = sim_op_4d(img_4d)

start = sim_img_shape[0] // 2 - recon_img_shape[0] // 2
end = start + recon_img_shape[0]
data_4d_cropped = data_4d[:, start:end, ...].copy()

# the data also needs to be scaled because of the oversampling
oversampling_factors = np.array(sim_img_shape) / np.array(recon_img_shape)
data_4d_cropped /= np.sqrt(np.prod(oversampling_factors))

# setup the operators for reconstruction (on a coarser grid)
# setup the operator that acts on a single 3D image
fwd_op_3d = stacked_nufft_operator(recon_img_shape,
                                   kspace_coords_2d.reshape(-1, 2))

# setup the operator that applies the 3d operator to a stack of 3 images
rs_in = sigpy.linop.Reshape(recon_img_shape, (1, ) + recon_img_shape)
rs_out = sigpy.linop.Reshape((1, ) + tuple(fwd_op_3d.oshape), fwd_op_3d.oshape)
ops = img_4d.shape[0] * [rs_out * fwd_op_3d * rs_in]
fwd_op_4d = sigpy.linop.Diag(ops, iaxis=0, oaxis=0)

#---------------------------------------------------------------------------
#---------------------------------------------------------------------------
# individual reconstructions with TV prior
#---------------------------------------------------------------------------
#---------------------------------------------------------------------------

# setup that 3D gradient operator that acts on a single 3D gate
G_3d = sigpy.linop.Gradient(recon_img_shape)

# setup a "stacked" 4D gradient operator that calculated the 3D gradients of all gates
# and stacks them into a 4D array
rs_in = sigpy.linop.Reshape(recon_img_shape, (1, ) + recon_img_shape)
rs_out = sigpy.linop.Reshape((1, ) + tuple(G_3d.oshape), G_3d.oshape)
G_ops = img_4d.shape[0] * [rs_out * G_3d * rs_in]
stacked_G = sigpy.linop.Diag(G_ops, iaxis=0, oaxis=0)

# weight of the TV prior term
beta = 1e-2

# (1) individual reconstructions with TV prior using a single algorithm and "stacked"
#     4D operators
alg = sigpy.app.LinearLeastSquares(fwd_op_4d,
                                   data_4d_cropped,
                                   G=stacked_G,
                                   proxg=sigpy.prox.L1Reg(
                                       stacked_G.oshape, beta),
                                   max_iter=1000)

ind_recons = alg.run()

# (2) individual reconstructions with TV prior using multiple algorithms and 3d operators
#     this should give the same result as (1) (up to numerical precision)
ind_recons2 = np.zeros_like(ind_recons)

for i in range(data_4d_cropped.shape[0]):
    # extract the 3D forward operator for the 4D composite fwd operator (removing the reshapes)
    op_3d = sigpy.linop.Compose(fwd_op_4d.linops[i].linops[1:-1])

    alg2 = sigpy.app.LinearLeastSquares(op_3d,
                                        data_4d_cropped[i, ...],
                                        G=G_3d,
                                        proxg=sigpy.prox.L1Reg(
                                            G_3d.oshape, beta),
                                        max_iter=1000)

    ind_recons2[i, ...] = alg2.run()

#---------------------------------------------------------------------------
#---------------------------------------------------------------------------
# ADMM for joint recon
#---------------------------------------------------------------------------
#---------------------------------------------------------------------------

## setup algorithm to solve ADMM subproblem (1)
## sigpy's LinearLeastSquares solves:
## min_x 0.5 * || fwd_op * x - data ||_2^2 + 0.5 * lambda * || x - z ||_2^2
## https://sigpy.readthedocs.io/en/latest/generated/sigpy.app.LinearLeastSquares.html#sigpy.app.LinearLeastSquares
## for this problem, sigpy uses conjugate gradient
#x0 = np.zeros(fwd_op_4d.ishape, dtype=np.complex128)

## setup a random 4D "bias" term for the quadratic penalty
#b_4d = gaussian_filter(
#    np.random.rand(*fwd_op_4d.ishape) +
#    1j * np.random.rand(*fwd_op_4d.ishape), 4)
#b_4d /= np.abs(b_4d).max()

#alg = sigpy.app.LinearLeastSquares(fwd_op_4d,
#                                   data_4d_cropped,
#                                   x=x0,
#                                   max_iter=50,
#                                   z=b_4d,
#                                   lamda=lam,
#                                   save_objective_values=True)

## run the algorithm
#res = alg.run()

## setup the cost function manually to double check that we optimize what we want
#cost = lambda x: 0.5 * (np.abs(fwd_op_4d(x) - data_4d_cropped)**2).sum(
#) + 0.5 * lam * (np.abs(x - b_4d)**2).sum()

#assert (np.isclose(cost(res), alg.objective_values[-1]))

#fig, ax = plt.subplots(1, 2, figsize=(8, 4))
#ax[0].plot(kspace_coords_2d[..., 0].T, kspace_coords_2d[..., 1].T)
#ax[1].semilogy(alg.objective_values)
#ax[1].set_ylim(0, max(alg.objective_values[1:]))
#fig.tight_layout()
#fig.show()