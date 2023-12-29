#!/usr/bin/env python
"""Compute the individual cross-exposure 1D power spectra
"""

import sys, os, argparse, glob
import fitsio
import numpy as np
import itertools
from picca.pk1d.compute_pk1d import compute_pk_cross_exposure,Pk1D


def read_delta_k_file(filename,args):
    fft_delta_list = []
    file_out = None
    with fitsio.FITS(filename, "r") as hdus:
        for i, hdu in enumerate(hdus[1:]):
            fft_delta = Pk1D.from_fitsio(hdu)  
            fft_delta_list.append(fft_delta)


    targetid_list = np.array(
        [fft_delta_list[i].los_id for i in range(len(fft_delta_list))]
    )
    chunkid_list = np.array(
        [fft_delta_list[i].chunk_id for i in range(len(fft_delta_list))]
    )

    unique_targetid = np.unique(targetid_list)
    unique_chunkid = np.unique(chunkid_list)
    for los_id in unique_targetid:
        for chunk_id in unique_chunkid:
            index = np.argwhere(
                (targetid_list == los_id) & (chunkid_list == chunk_id)
            )
            if len(index) < 2:
                continue
            index = np.concatenate(index, axis=0)
            fft_delta_real = np.array(
                    [fft_delta_list[i].fft_delta_real for i in index]
                )
            fft_delta_imag = np.array(
                    [fft_delta_list[i].fft_delta_imag for i in index]
                )

            ra = fft_delta_list[0].ra
            dec = fft_delta_list[0].dec
            z_qso = fft_delta_list[0].z_qso
            mean_z = fft_delta_list[0].mean_z
            num_masked_pixels = fft_delta_list[0].num_masked_pixels
            linear_bining = fft_delta_list[0].linear_bining

            k = fft_delta_list[0].k

            mean_snr = np.mean(
                    [fft_delta_list[i].mean_snr for i in index]
                )       
            mean_reso = np.mean(
                    [fft_delta_list[i].mean_reso for i in index]
                )               

            pk_noise = np.mean(
                    [fft_delta_list[i].pk_noise for i in index], axis=0
                )    
            pk_diff = np.mean(
                    [fft_delta_list[i].pk_diff for i in index], axis=0
                )  
            correction_reso = np.mean(
                    [fft_delta_list[i].correction_reso for i in index], axis=0
                )          

            pk_raw_cross_exposure = compute_pk_cross_exposure(
                fft_delta_real, fft_delta_imag
            )

            pk_cross_exposure = pk_raw_cross_exposure/correction_reso

            pk1d_class = Pk1D(
                    ra=ra,
                    dec=dec,
                    z_qso=z_qso,
                    mean_z=mean_z,
                    mean_snr=mean_snr,
                    mean_reso=mean_reso,
                    num_masked_pixels=num_masked_pixels,
                    linear_bining=linear_bining,
                    los_id=los_id,
                    chunk_id=chunk_id,
                    k=k,
                    pk_raw=pk_raw_cross_exposure,
                    pk_noise=pk_noise,
                    pk_diff=pk_diff,
                    correction_reso=correction_reso,
                    pk=pk_cross_exposure,
                )
            file_index =0
            if file_out is None:
                file_out = fitsio.FITS((args.out_dir + '/Pk1D-' +
                                           str(file_index) + '.fits.gz'),
                                          'rw',
                                          clobber=True)
                                    
            pk1d_class.write_fits(file_out)


def main(cmdargs):
    """Compute the averaged 1D power spectrum"""

    parser = argparse.ArgumentParser(
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        description="Compute the averaged 1D power spectrum",
    )

    parser.add_argument(
        "--in-dir",
        type=str,
        default=None,
        required=True,
        help="Directory to individual fft delta files",
    )

    parser.add_argument(
        "--out-dir",
        type=str,
        default=None,
        required=True,
        help="Directory to individual P1D files",
    )

    args = parser.parse_args(cmdargs)

    os.makedirs(args.out_dir, exist_ok=True)

    files = glob.glob(os.path.join(args.in_dir, f"Deltak-*.fits.gz"))

    for filename in files[:2]:
        read_delta_k_file(filename,args)


if __name__ == "__main__":
    cmdargs = sys.argv[1:]
    main(cmdargs)
