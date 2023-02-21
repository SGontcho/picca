"""This module defines a set of functions to postprocess files produced by compute_pk1d.py.

This module provides 3 main functions:
    - read_pk1d:
        Read all HDUs in an individual "P1D" FITS file and stacks
        all data in one table
    - compute_mean_pk1d:
        Compute the mean P1D in a given (z,k) grid of bins, from individual
        "P1Ds" of individual chunks
    - parallelize_p1d_comp:
        Main function, runs read_pk1d in parallel, then runs compute_mean_pk1d
See the respective documentation for details
"""

import os
import glob
from multiprocessing import Pool
from multiprocessing.pool import ThreadPool

from functools import partial
import numpy as np
from scipy.stats import binned_statistic
from scipy.optimize import curve_fit
import fitsio
from astropy.table import Table, vstack
from astropy.stats import bootstrap

from picca.constants import SPEED_LIGHT
from picca.constants import ABSORBER_IGM
from picca.utils import userprint
from picca.pk1d.utils import MEANPK_FITRANGE_SNR, fitfunc_variance_pk1d


def read_pk1d(filename, kbin_edges, snrcut=None, zbins_snrcut=None):
    """Read Pk1D data from a single file

    Arguments
    ---------
    filename: string
    Fits file, containing individual "P1D" for each chunk

    kbin_edges: array of floats
    Edges of the wavenumber bins to be later used, in Angstrom^-1

    snrcut: array of floats or None
    Chunks with mean SNR > snrcut are discarded. If len(snrcut)>1,
    zbins_snrcut must be set, so that the cut is made redshift dependent.

    zbins_snrcut: array of floats or None
    Required if len(snrcut)>1. List of redshifts
    associated to the list of snr cuts.

    Return
    ------
    p1d_table: Table
    One entry per mode(k) per chunk

    z_array: array of floats
    Nchunks entry.
    If no chunk is selected, None will be returned instead
    """
    p1d_table = []
    z_array = []
    with fitsio.FITS(filename, "r") as hdus:
        for i, hdu in enumerate(hdus[1:]):
            data = hdu.read()
            chunk_header = hdu.read_header()
            chunk_table = Table(data)
            for colname in [
                "k",
                "Pk",
                "Pk_raw",
                "Pk_noise",
                "Pk_diff",
                "cor_reso",
                "Pk_noise_miss",
            ]:
                try:
                    chunk_table.rename_column(colname.upper(), colname)
                except KeyError:
                    pass

            if np.nansum(chunk_table["Pk"]) == 0:
                chunk_table["Pk"] = (
                    chunk_table["Pk_raw"] - chunk_table["Pk_noise"]
                ) / chunk_table["cor_reso"]

            chunk_table["forest_z"] = float(chunk_header["MEANZ"])
            chunk_table["forest_snr"] = float(chunk_header["MEANSNR"])
            chunk_table["forest_id"] = int(chunk_header["LOS_ID"])
            if "CHUNK_ID" in chunk_header:
                chunk_table[
                    "sub_forest_id"
                ] = f"{chunk_header['LOS_ID']}_{chunk_header['CHUNK_ID']}"

            if snrcut is not None:
                if len(snrcut) > 1:
                    if (zbins_snrcut is None) or (len(zbins_snrcut) != len(snrcut)):
                        raise ValueError(
                            "Please provide same size for zbins_snrcut and snrcut arrays"
                        )
                    zbin_index = np.argmin(np.abs(zbins_snrcut - chunk_header["MEANZ"]))
                    snrcut_chunk = snrcut[zbin_index]
                else:
                    snrcut_chunk = snrcut[0]
                if chunk_header["MEANSNR"] < snrcut_chunk:
                    continue

            # Empirically remove very noisy chunks
            (wk,) = np.where(chunk_table["k"] < kbin_edges[-1])
            if (
                chunk_table["Pk_noise"][wk] > 1000000 * chunk_table["Pk_raw"][wk]
            ).any():
                userprint(
                    f"file {filename} hdu {i+1} has very high noise power: discarded"
                )
                continue

            p1d_table.append(chunk_table)
            z_array.append(float(chunk_header["MEANZ"]))

    if len(p1d_table) == 0:  # No chunk was selected
        return None

    p1d_table = vstack(p1d_table)
    p1d_table["Delta2"] = p1d_table["k"] * p1d_table["Pk"] / np.pi
    p1d_table["Pk_norescor"] = p1d_table["Pk_raw"] - p1d_table["Pk_noise"]
    p1d_table["Pk_nonoise"] = p1d_table["Pk_raw"] / p1d_table["cor_reso"]
    p1d_table["Pk_noraw"] = p1d_table["Pk_noise"] / p1d_table["cor_reso"]
    try:
        p1d_table["Pk_noraw_miss"] = p1d_table["Pk_noise_miss"] / p1d_table["cor_reso"]
    except KeyError:
        pass

    z_array = np.array(z_array)

    return p1d_table, z_array


def compute_mean_pk1d(
    p1d_table,
    z_array,
    zbin_edges,
    kbin_edges,
    weight_method,
    apply_z_weights=False,
    nomedians=False,
    velunits=False,
    output_snrfit=None,
    compute_covariance=False,
    compute_bootstrap=False,
    number_bootstrap=50,
    number_worker=8,
):
    """Compute mean P1D in a set of given (z,k) bins, from individual chunks P1Ds

    Arguments
    ---------
    p1d_table: Table
    Individual Pk1Ds of the contributing forest chunks, stacked in one table using "read_pk1d",
    Contains 'k', 'Pk_raw', 'Pk_noise', 'Pk_diff', 'cor_reso', 'Pk', 'forest_z', 'forest_snr',
            'Delta2', 'Pk_norescor', 'Pk_nonoise', 'Pk_noraw'

    z_array: Array of float
    Mean z of each contributing chunk, stacked in one array using "read_pk1d"

    zbin_edges: Array of float
    Edges of the redshift bins we want to use

    kbin_edges: Array of float
    Edges of the wavenumber bins we want to use, either in (Angstrom)-1 or s/km

    weight_method: string
    3 possible options:
        'fit_snr': Compute mean P1D with weights estimated by fitting dispersion vs SNR
        'no_weights': Compute mean P1D without weights
        'simple_snr' (obsolete): Compute mean P1D with weights computed directly from SNR values
                    (SNR as given in compute_Pk1D outputs)

    apply_z_weights: Bool
    If True, each chunk contributes to two nearest redshift bins with a linear weighting scheme.

    nomedians: Bool
    Skip computation of median quantities

    velunits: Bool
    Compute P1D in velocity units by converting k on-the-fly from AA-1 to s/km

    output_snrfit: string
    If weight_method='fit_snr', the results of the fit can be saved to an ASCII file.
    The file contains (z k a b standard_dev_points) for the "Pk" variable, for each (z,k) point

    Return
    ------
    meanP1d_table: Table
    One row per (z,k) bin; one column per statistics (eg. meanPk, errorPk_noise...)
    Other columns: 'N' (nb of chunks used), 'index_zbin' (index of associated
    row in metadata_table), 'zbin'

    metadata_table: Table
    One row per z bin; column values z_min/max, k_min/max, N_chunks
    """
    # Initializing stats we want to compute on data
    stats_array = ["mean", "error", "min", "max"]
    if not nomedians:
        stats_array += ["median"]

    p1d_table_cols = p1d_table.colnames
    p1d_table_cols.remove("forest_id")
    if "sub_forest_id" in p1d_table_cols:
        p1d_table_cols.remove("sub_forest_id")

    # Convert data into velocity units
    if velunits:
        conversion_factor = (
            ABSORBER_IGM["LYA"] * (1.0 + p1d_table["forest_z"])
        ) / SPEED_LIGHT
        p1d_table["k"] *= conversion_factor
        for col in p1d_table_cols:
            if "Pk" in col:
                p1d_table[col] /= conversion_factor

    # Initialize mean_p1d_table of len = (nzbins * nkbins) corresponding to hdu[1] in final ouput
    mean_p1d_table = Table()
    nbins_z, nbins_k = len(zbin_edges) - 1, len(kbin_edges) - 1
    mean_p1d_table["zbin"] = np.zeros(nbins_z * nbins_k)
    mean_p1d_table["index_zbin"] = np.zeros(nbins_z * nbins_k, dtype=int)
    mean_p1d_table["N"] = np.zeros(nbins_z * nbins_k, dtype=int)
    for col in p1d_table_cols:
        for stats in stats_array:
            mean_p1d_table[stats + col] = np.ones(nbins_z * nbins_k) * np.nan

    # Initialize metadata_table of len = nbins_z corresponding to hdu[2] in final output
    metadata_table = Table()
    metadata_table["z_min"] = zbin_edges[:-1]
    metadata_table["z_max"] = zbin_edges[1:]
    metadata_table["k_min"] = kbin_edges[0] * np.ones(nbins_z)
    metadata_table["k_max"] = kbin_edges[-1] * np.ones(nbins_z)

    if (compute_covariance) | (compute_bootstrap):
        if "sub_forest_id" not in p1d_table.columns:
            userprint(
                """sub_forest_id cannot be computed from individual pk files,
                necessary to compute covariance. Skipping calculation"""
            )
            compute_covariance, compute_bootstrap = False, False
            cov_table = None

        elif weight_method != "no_weights":
            userprint(
                """Covariance calculations are not compatible with SNR weighting method yet.
                Skipping calculation"""
            )

            compute_covariance, compute_bootstrap = False, False
            cov_table = None

        elif apply_z_weights:
            userprint(
                """Covariance calculations are not compatible redshift weighting yes.
                Skipping calculation"""
            )
            compute_covariance, compute_bootstrap = False, False
            cov_table = None

        else:
            # Initialize cov_table of len = (nzbins * nkbins * nkbins)
            # corresponding to hdu[3] in final ouput
            cov_table = Table()
            cov_table["zbin"] = np.zeros(nbins_z * nbins_k * nbins_k)
            cov_table["index_zbin"] = np.zeros(nbins_z * nbins_k * nbins_k, dtype=int)
            cov_table["N"] = np.zeros(nbins_z * nbins_k * nbins_k, dtype=int)
            cov_table["covariance"] = np.zeros(nbins_z * nbins_k * nbins_k)

            if compute_bootstrap:
                cov_table["boot_covariance"] = np.zeros(nbins_z * nbins_k * nbins_k)
                cov_table["error_boot_covariance"] = np.zeros(
                    nbins_z * nbins_k * nbins_k
                )

            k_index = np.full(len(p1d_table["k"]), -1, dtype=int)
            for ikbin, _ in enumerate(kbin_edges[:-1]):  # First loop 1) k bins
                select = (p1d_table["k"] < kbin_edges[ikbin + 1]) & (
                    p1d_table["k"] > kbin_edges[ikbin]
                )  # select a specific k bin
                k_index[select] = ikbin
    else:
        cov_table = None

    # Number of chunks in each redshift bin
    n_chunks, _, _ = binned_statistic(
        z_array, z_array, statistic="count", bins=zbin_edges
    )
    metadata_table["N_chunks"] = n_chunks

    zbin_centers = np.around((zbin_edges[1:] + zbin_edges[:-1]) / 2, 5)
    if output_snrfit is not None:
        snrfit_table = np.zeros(
            (nbins_z * nbins_k, 13)
        )  # 13 entries: z k a b + 9 SNR bins used for the fit
    else:
        snrfit_table = None

    userprint("Computing average p1d")
    # Main loop 1) z bins
    params_pool = [izbin for izbin, _ in enumerate(zbin_edges[:-1])]

    func = partial(
        fill_average_pk_redshift,
        p1d_table,
        p1d_table_cols,
        mean_p1d_table,
        weight_method,
        apply_z_weights,
        snrfit_table,
        output_snrfit,
        nomedians,
        nbins_z,
        zbin_centers,
        zbin_edges,
        n_chunks,
        nbins_k,
        kbin_edges,
    )
    if number_worker == 1:
        for p in params_pool:
            func(p)
    else:
        with ThreadPool(number_worker) as pool:
            pool.map(func, params_pool)

    if compute_covariance:
        userprint("Computing covariance matrix")
        params_pool = []
        for izbin in range(nbins_z):  # Main loop 1) z bins
            select_z = (p1d_table["forest_z"] < zbin_edges[izbin + 1]) & (
                p1d_table["forest_z"] > zbin_edges[izbin]
            )
            sub_forest_ids = np.unique(p1d_table["sub_forest_id"][select_z])
            params_pool.append([izbin, select_z, sub_forest_ids])

        func = partial(
            compute_cov,
            p1d_table,
            mean_p1d_table,
            zbin_centers,
            n_chunks,
            k_index,
            nbins_k,
        )
        if number_worker == 1:
            output_cov = [func(p) for p in params_pool]
        else:
            with Pool(number_worker) as pool:
                output_cov = pool.starmap(func, params_pool)

        for izbin in range(nbins_z):  # Main loop 1) z bins
            zbin_array, index_zbin_array, n_array, covariance_array = (
                *output_cov[izbin],
            )
            i_min = izbin * nbins_k * nbins_k
            i_max = (izbin + 1) * nbins_k * nbins_k
            cov_table["zbin"][i_min:i_max] = zbin_array
            cov_table["index_zbin"][i_min:i_max] = index_zbin_array
            cov_table["N"][i_min:i_max] = n_array
            cov_table["covariance"][i_min:i_max] = covariance_array

    if compute_bootstrap:
        userprint("Computing covariance matrix with bootstrap method")

        params_pool = []
        for izbin in range(nbins_z):  # Main loop 1) z bins - can be paralelized
            select_z = (p1d_table["forest_z"] < zbin_edges[izbin + 1]) & (
                p1d_table["forest_z"] > zbin_edges[izbin]
            )

            sub_forest_ids = np.unique(p1d_table["sub_forest_id"][select_z])
            bootid = np.array(
                bootstrap(np.arange(sub_forest_ids.size), number_bootstrap)
            ).astype(int)
            for iboot in range(
                number_bootstrap
            ):  # Main loop 2) number of bootstrap samples - can be paralelized
                params_pool.append([izbin, select_z, sub_forest_ids[bootid[iboot]]])

        func = partial(
            compute_cov,
            p1d_table,
            mean_p1d_table,
            zbin_centers,
            n_chunks,
            k_index,
            nbins_k,
        )
        if number_worker == 1:
            output_cov = [func(p) for p in params_pool]
        else:
            with Pool(number_worker) as pool:
                output_cov = pool.starmap(func, params_pool)

        for izbin in range(nbins_z):  # Main loop 1) z bins - can be paralelized
            boot_cov = []
            for iboot in range(
                number_bootstrap
            ):  # Main loop 2) number of bootstrap samples - can be paralelized
                zbin_array, index_zbin_array, n_array, covariance_array = (
                    *output_cov[izbin * number_bootstrap + iboot],
                )
                boot_cov.append(covariance_array)

            i_min = izbin * nbins_k * nbins_k
            i_max = (izbin + 1) * nbins_k * nbins_k
            cov_table["boot_covariance"][i_min:i_max] = np.mean(boot_cov, axis=0)
            cov_table["error_boot_covariance"][i_min:i_max] = np.std(boot_cov, axis=0)

    if output_snrfit is not None:
        np.savetxt(
            output_snrfit,
            snrfit_table,
            fmt="%.5e",
            header="Result of fit: Variance(Pks) vs SNR\n"
            "SNR bin edges used: 1,  2,  3,  4,  5,  6,  7,  8,  9, 10\n"
            "z k a b standard_dev_points",
        )

    return mean_p1d_table, metadata_table, cov_table


def fill_average_pk_redshift(
    p1d_table,
    p1d_table_cols,
    mean_p1d_table,
    weight_method,
    apply_z_weights,
    snrfit_table,
    output_snrfit,
    nomedians,
    nbins_z,
    zbin_centers,
    zbin_edges,
    n_chunks,
    nbins_k,
    kbin_edges,
    izbin,
):

    """Fill the average P1D table for the given redshift and k bins.

    The function fills the mean P1D table for each redshift and k bin.
    If there are no chunks in a given bin, the rows in the
    table for that bin will be filled with NaNs.
    The mean value for each bin is calculated using a weighting method,
    either a fit to the SNR or using weights based on the redshift.


    Arguments
    ---------
    p1d_table: numpy ndarray,
    Table containing the data to be averaged.

    p1d_table_cols: List of str,
    Column names in the input table to be averaged.

    mean_p1d_table: astropy Table,
    Output table to be filled with the mean values.

    weight_method: str,
    Method to weight the data.

    apply_z_weights: bool,
    If True, apply redshift weights.

    snrfit_table: numpy ndarray,
    Table containing the fit to the SNR.

    output_snrfit: bool,
    If not None, write the fit to the SNR to a file.

    nomedians: bool,
    If True, do not use median values in the fit to the SNR.

    nbins_z: int,
    Number of redshift bins.

    zbin_centers: numpy ndarray,
    Centers of the redshift bins.

    zbin_edges: numpy ndarray,
    Edges of the redshift bins.

    n_chunks: numpy ndarray,
    Number of chunks in each redshift bin.

    nbins_k: int,
    Number of k bins.

    kbin_edges: numpy ndarray,
    Edges of the k bins.

    izbin: int,
    Index of the current redshift bin.

    Return
    ------
    None
    """

    if n_chunks[izbin] == 0:  # Fill rows with NaNs
        i_min = izbin * nbins_k
        i_max = (izbin + 1) * nbins_k
        mean_p1d_table["zbin"][i_min:i_max] = zbin_centers[izbin]
        mean_p1d_table["index_zbin"][i_min:i_max] = izbin
        return

    for ikbin, kbin in enumerate(kbin_edges[:-1]):  # Main loop 2) k bins

        if apply_z_weights:  # special chunk selection in that case
            delta_z = zbin_centers[1:] - zbin_centers[:-1]
            if not np.allclose(delta_z, delta_z[0], atol=1.0e-3):
                raise ValueError(
                    "z bins should have equal widths with apply_z_weights."
                )
            delta_z = delta_z[0]

            select = (p1d_table["k"] < kbin_edges[ikbin + 1]) & (
                p1d_table["k"] > kbin_edges[ikbin]
            )
            if izbin in (0, nbins_z - 1):
                # First and last bin: in order to avoid edge effects,
                #    use only chunks within the bin
                select = (
                    select
                    & (p1d_table["forest_z"] > zbin_edges[izbin])
                    & (p1d_table["forest_z"] < zbin_edges[izbin + 1])
                )
            else:
                select = (
                    select
                    & (p1d_table["forest_z"] < zbin_centers[izbin + 1])
                    & (p1d_table["forest_z"] > zbin_centers[izbin - 1])
                )

            redshift_weights = (
                1.0
                - np.abs(p1d_table["forest_z"][select] - zbin_centers[izbin]) / delta_z
            )

        else:
            select = (
                (p1d_table["forest_z"] < zbin_edges[izbin + 1])
                & (p1d_table["forest_z"] > zbin_edges[izbin])
                & (p1d_table["k"] < kbin_edges[ikbin + 1])
                & (p1d_table["k"] > kbin_edges[ikbin])
            )  # select a specific (z,k) bin

        index = (nbins_k * izbin) + ikbin  # index to be filled in table
        mean_p1d_table["zbin"][index] = zbin_centers[izbin]
        mean_p1d_table["index_zbin"][index] = izbin

        # Counts the number of chunks in each (z,k) bin
        num_chunks = np.ma.count(p1d_table["k"][select])

        mean_p1d_table["N"][index] = num_chunks

        for col in p1d_table_cols:

            if num_chunks == 0:
                userprint(
                    "Warning: 0 chunks found in bin "
                    + str(zbin_edges[izbin])
                    + "<z<"
                    + str(zbin_edges[izbin + 1])
                    + ", "
                    + str(kbin_edges[ikbin])
                    + "<k<"
                    + str(kbin_edges[ikbin + 1])
                )
                continue

            if weight_method == "fit_snr":
                snr_bin_edges = np.arange(
                    MEANPK_FITRANGE_SNR[0], MEANPK_FITRANGE_SNR[1] + 1, 1
                )
                snr_bins = (snr_bin_edges[:-1] + snr_bin_edges[1:]) / 2

                data_values = p1d_table[col][select]
                data_snr = p1d_table["forest_snr"][select]
                mask = np.isnan(data_values)
                if len(mask[mask]) != 0:
                    userprint(
                        "Warning: A nan value was detected in the following table:\n",
                        data_values[mask],
                    )
                    data_snr = data_snr[~mask]
                    data_values = data_values[~mask]
                # Fit function to observed dispersion:
                standard_dev, _, _ = binned_statistic(
                    data_snr, data_values, statistic="std", bins=snr_bin_edges
                )
                # the *_ is to ignore the rest of the return arguments
                coef, *_ = curve_fit(
                    fitfunc_variance_pk1d,
                    snr_bins,
                    standard_dev**2,
                    bounds=(0, np.inf),
                )

                # Model variance from fit function
                data_snr[data_snr > MEANPK_FITRANGE_SNR[1]] = MEANPK_FITRANGE_SNR[1]
                data_snr[data_snr < 1.01] = 1.01
                variance_estimated = fitfunc_variance_pk1d(data_snr, *coef)
                weights = 1.0 / variance_estimated
                if apply_z_weights:
                    weights *= redshift_weights
                mean = np.average(data_values, weights=weights)
                if apply_z_weights:
                    # Analytic expression for the re-weighted average:
                    error = np.sqrt(np.sum(weights * redshift_weights)) / np.sum(
                        weights
                    )
                else:
                    error = np.sqrt(1.0 / np.sum(weights))
                if output_snrfit is not None and col == "Pk":
                    snrfit_table[index, 0:4] = [
                        zbin_centers[izbin],
                        (kbin + kbin_edges[ikbin + 1]) / 2.0,
                        coef[0],
                        coef[1],
                    ]
                    snrfit_table[index, 4:] = standard_dev

            elif weight_method == "simple_snr":
                # - We keep this for record, we do not recommand to use it
                # for forests with snr>snr_limit,
                # the weight is fixed to (snr_limit - 1)**2 = 9
                snr_limit = 4
                forest_snr = p1d_table["forest_snr"][select]
                # w, = np.where(forest_snr <= 1)
                # if len(w)>0: raise RuntimeError('Cannot add weights with SNR<=1.')
                if (forest_snr <= 1).sum() > 0:
                    raise RuntimeError("Cannot add weights with SNR<=1.")
                weights = (forest_snr - 1) ** 2
                weights[forest_snr > snr_limit] = (snr_limit - 1) ** 2
                mean = np.average(p1d_table[col][select], weights=weights)
                # Need to rescale the weights to find the error:
                #   weights_true = weights * (num_chunks - 1) / alpha
                alpha = np.sum(weights * ((p1d_table[col][select] - mean) ** 2))
                error = np.sqrt(alpha / (np.sum(weights) * (num_chunks - 1)))

            elif weight_method == "no_weights":
                if apply_z_weights:
                    mean = np.average(p1d_table[col][select], weights=redshift_weights)
                    # simple analytic expression:
                    error = np.std(p1d_table[col][select]) * (
                        np.sqrt(np.sum(redshift_weights**2))
                        / np.sum(redshift_weights)
                    )
                else:
                    mean = np.mean(p1d_table[col][select])
                    # unbiased estimate: num_chunks-1
                    error = np.std(p1d_table[col][select]) / np.sqrt(num_chunks - 1)

            else:
                raise ValueError("Option for 'weight_method' argument not found")

            minimum = np.min((p1d_table[col][select]))
            maximum = np.max((p1d_table[col][select]))

            mean_p1d_table["mean" + col][index] = mean
            mean_p1d_table["error" + col][index] = error
            mean_p1d_table["min" + col][index] = minimum
            mean_p1d_table["max" + col][index] = maximum
            if not nomedians:
                median = np.median((p1d_table[col][select]))
                mean_p1d_table["median" + col][index] = median


def compute_cov(
    p1d_table,
    mean_p1d_table,
    zbin_centers,
    n_chunks,
    k_index,
    nbins_k,
    izbin,
    select_z,
    sub_forest_ids,
):
    """Computes the covariance of a set of 1D power spectra.


    Arguments
    ---------
    p1d_table (array-like):
    Table of 1D power spectra, with columns 'Pk' and 'sub_forest_id'.

    mean_p1d_table (array-like):
    Table of mean 1D power spectra, with column 'meanPk'.

    zbin_centers (array-like):
    Array of bin centers for redshift.

    n_chunks (array-like):
    Array of the number of chunks in each redshift bin.

    k_index (array-like):
    Array of indices for k-values, with -1 indicating values outside of the k bins.

    nbins_k (int):
    Number of k bins.

    izbin (int):
    Current redshift bin being considered.

    select_z (array-like):
    Boolean array for selecting data points based on redshift.

    sub_forest_ids (array-like):
    Array of chunk ids.

    Return
    ------
    zbin_array (array-like):
    Array of redshift bin centers for each covariance coefficient.

    index_zbin_array (array-like):
    Array of redshift bin indices for each covariance coefficient.

    n_array (array-like):
    Array of the number of power spectra used to compute each covariance coefficient.

    covariance_array (array-like):
    Array of covariance coefficients.
    """

    zbin_array = np.zeros(nbins_k * nbins_k)
    index_zbin_array = np.zeros(nbins_k * nbins_k, dtype=int)
    n_array = np.zeros(nbins_k * nbins_k, dtype=int)
    covariance_array = np.zeros(nbins_k * nbins_k)

    if n_chunks[izbin] == 0:  # Fill rows with NaNs
        zbin_array[:] = zbin_centers[izbin]
        index_zbin_array[:] = izbin
        n_array[:] = 0
        covariance_array[:] = np.nan
        return zbin_array, index_zbin_array, n_array, covariance_array

    for sub_forest_id in sub_forest_ids:  # First loop 1) id sub-forest bins
        select_id = select_z & (p1d_table["sub_forest_id"] == sub_forest_id)
        selected_pk = p1d_table["Pk"][select_id]
        selected_ikbin = k_index[select_id]

        for ipk, _ in enumerate(selected_pk):  # First loop 2) selected pk
            ikbin = selected_ikbin[ipk]
            if ikbin != -1:
                for ipk2 in range(ipk, len(selected_pk)):  # First loop 3) selected pk
                    ikbin2 = selected_ikbin[ipk2]

                    if ikbin2 != -1:
                        # index of the (ikbin,ikbin2) coefficient on the top of the matrix
                        index = (nbins_k * ikbin) + ikbin2
                        covariance_array[index] = (
                            covariance_array[index]
                            + selected_pk[ipk] * selected_pk[ipk2]
                        )
                        n_array[index] = n_array[index] + 1

    for ikbin in range(nbins_k):  # Second loop 1) k bins
        mean_ikbin = mean_p1d_table["meanPk"][(nbins_k * izbin) + ikbin]

        for ikbin2 in range(ikbin, nbins_k):  # Second loop 2) k bins
            mean_ikbin2 = mean_p1d_table["meanPk"][(nbins_k * izbin) + ikbin2]

            # index of the (ikbin,ikbin2) coefficient on the top of the matrix
            index = (nbins_k * ikbin) + ikbin2
            covariance_array[index] = (
                (covariance_array[index] / n_array[index]) - mean_ikbin * mean_ikbin2
            ) / n_array[index]

            zbin_array[index] = zbin_centers[izbin]
            index_zbin_array[index] = izbin

            if ikbin2 != ikbin:
                # index of the (ikbin,ikbin2) coefficient on the bottom of the matrix
                index_2 = (nbins_k * ikbin2) + ikbin
                covariance_array[index_2] = covariance_array[index]
                n_array[index_2] = n_array[index]

                zbin_array[index_2] = zbin_centers[izbin]
                index_zbin_array[index_2] = izbin

    return zbin_array, index_zbin_array, n_array, covariance_array


def run_postproc_pk1d(
    data_dir,
    output_file,
    zbin_edges,
    kbin_edges,
    weight_method="no_weights",
    apply_z_weights=False,
    snrcut=None,
    zbins_snrcut=None,
    output_snrfit=None,
    nomedians=False,
    velunits=False,
    overwrite=False,
    ncpu=8,
    compute_covariance=False,
    compute_bootstrap=False,
    number_bootstrap=50,
):
    """Read individual Pk1D data from a set of files and compute P1D statistics,
    stored in a summary FITS file.

    Arguments
    ---------
    data_dir: string
    Directory where individual P1D FITS files are located

    output_file: string
    Output file name

    overwrite: Bool
    Overwrite output file if existing

    ncpu: int
    The I/O function read_pk1d() is run parallel

    Other arguments are as defined
    in compute_mean_pk1d() or read_pk1d()
    """

    if os.path.exists(output_file) and not overwrite:
        raise RuntimeError("Output file already exists: " + output_file)

    searchstr = "*"
    files = glob.glob(os.path.join(data_dir, f"Pk1D{searchstr}.fits.gz"))

    with Pool(ncpu) as pool:
        output_readpk1d = pool.starmap(
            read_pk1d, [[f, kbin_edges, snrcut, zbins_snrcut] for f in files]
        )

    output_readpk1d = [x for x in output_readpk1d if x is not None]
    p1d_table = vstack([output_readpk1d[i][0] for i in range(len(output_readpk1d))])
    z_array = np.concatenate(
        tuple(output_readpk1d[i][1] for i in range(len(output_readpk1d)))
    )

    userprint("Individual P1Ds read, now computing statistics.")

    mean_p1d_table, metadata_table, cov_table = compute_mean_pk1d(
        p1d_table,
        z_array,
        zbin_edges,
        kbin_edges,
        weight_method,
        apply_z_weights,
        nomedians=nomedians,
        velunits=velunits,
        output_snrfit=output_snrfit,
        compute_covariance=compute_covariance,
        compute_bootstrap=compute_bootstrap,
        number_bootstrap=number_bootstrap,
    )

    result = fitsio.FITS(output_file, "rw", clobber=True)
    result.write(mean_p1d_table.as_array())
    result.write(
        metadata_table.as_array(),
        header={"VELUNITS": velunits, "NQSO": len(np.unique(p1d_table["forest_id"]))},
    )
    if cov_table is not None:
        result.write(cov_table.as_array())
    result.close()
