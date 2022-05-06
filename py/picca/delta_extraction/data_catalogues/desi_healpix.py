"""This module defines the class DesiData to load DESI data
"""
import logging
import os
import multiprocessing

import fitsio
import numpy as np

from picca.delta_extraction.astronomical_objects.desi_forest import DesiForest
from picca.delta_extraction.astronomical_objects.desi_pk1d_forest import DesiPk1dForest
from picca.delta_extraction.astronomical_objects.forest import Forest
from picca.delta_extraction.utils_pk1d import spectral_resolution_desi, exp_diff_desi

from picca.delta_extraction.data_catalogues.desi_data import (
    DesiData, DesiDataFileHandler, merge_new_forest)
from picca.delta_extraction.data_catalogues.desi_data import (# pylint: disable=unused-import
    defaults, accepted_options)
from picca.delta_extraction.errors import DataError

accepted_options = sorted(
    list(set(accepted_options + ["num processors"])))


class DesiHealpix(DesiData):
    """Reads the spectra from DESI using healpix mode and formats its data as a
    list of Forest instances.

    Methods
    -------
    (see DesiData in py/picca/delta_extraction/data_catalogues/desi_data.py)
    __init__
    __parse_config
    get_filename
    read_data
    read_file

    Attributes
    ----------
    (see DesiData in py/picca/delta_extraction/data_catalogues/desi_data.py)

    logger: logging.Logger
    Logger object

    num_processors: int
    Number of processors to be used for parallel reading
    """

    def __init__(self, config):
        """Initialize class instance

        Arguments
        ---------
        config: configparser.SectionProxy
        Parsed options to initialize class
        """
        self.logger = logging.getLogger(__name__)

        self.num_processors = None
        self.__parse_config(config)

        # init of DesiData needs to come last, as it contains the actual data
        # reading and thus needs all config
        super().__init__(config)

        if self.analysis_type == "PK 1D":
            DesiPk1dForest.update_class_variables()

    def __parse_config(self, config):
        """Parse the configuration options

        Arguments
        ---------
        config: configparser.SectionProxy
        Parsed options to initialize class

        Raise
        -----
        DataError upon missing required variables
        """
        self.num_processors = config.getint("num processors")
        if self.num_processors is None:
            raise DataError(
                "Missing argument 'num processors' required by DesiHealpix")
        if self.num_processors == 0:
            self.num_processors = (multiprocessing.cpu_count() // 2)

    def get_filename(self, survey, healpix):
        """Get the name of the file to read

        Arguments
        ---------
        survey: str
        Name of the survey (sv, sv1, sv2, sv3, main, special)

        healpix: int
        Healpix of observations

        Return
        ------
        filename: str
        The name of the file to read

        is_mock: bool
        False, as we are reading DESI data
        """
        input_directory = f'{self.input_directory}/{survey}/dark'
        coadd_name = "spectra" if self.use_non_coadded_spectra else "coadd"
        filename = (
            f"{input_directory}/{healpix//100}/{healpix}/{coadd_name}-{survey}-"
            f"dark-{healpix}.fits")
        # TODO: not sure if we want the dark survey to be hard coded
        # in here, probably won't run on anything else, but still
        return filename, False

    def read_data(self):
        """Read the data.

        Method used to read healpix-based survey data.

        Return
        ------
        is_mock: bool
        False for DESI data and True for mocks

        is_sv: bool
        True if all the read data belong to SV. False otherwise

        Raise
        -----
        DataError if no quasars were found
        """
        grouped_catalogue = self.catalogue.group_by(["HEALPIX", "SURVEY"])

        is_sv = True
        is_mock = False
        forests_by_targetid = {}

        arguments = []
        for (healpix, survey), group in zip(grouped_catalogue.groups.keys,
                                            grouped_catalogue.groups):
            if survey not in ["sv", "sv1", "sv2", "sv3"]:
                is_sv = False

            filename, is_mock_aux = self.get_filename(survey, healpix)
            if is_mock_aux:
                is_mock = True

            arguments.append((filename, group))

        self.logger.info(f"reading data from {len(arguments)} files")

        if self.num_processors > 1:
            context = multiprocessing.get_context('fork')
            pool = context.Pool(processes=self.num_processors)
            imap_it = pool.imap(DesiHealpixFileHandler(self.analysis_type, self.use_non_coadded_spectra, self.logger), arguments)
            for forests_by_pe in imap_it:
                # Merge each dict to master forests_by_targetid
                merge_new_forest(forests_by_targetid, forests_by_pe)

        else:
            reader = DesiHealpixFileHandler(self.analysis_type, self.use_non_coadded_spectra, self.logger)
            for index, this_arg in enumerate(arguments):
                self.logger.progress(
                    f"Read {index} of {len(arguments)}. "
                    f"num_data: {len(forests_by_targetid)}")

                merge_new_forest(forests_by_targetid, reader(this_arg))

        if len(forests_by_targetid) == 0:
            raise DataError("No quasars found, stopping here")
        self.forests = list(forests_by_targetid.values())

        return is_mock, is_sv


# Class to read in parallel
# Seems lightweight to copy all these 3 arguments
class DesiHealpixFileHandler(DesiDataFileHandler):
    def read_file(self, filename, catalogue):
        """Read the spectra and formats its data as Forest instances.

        Arguments
        ---------
        filename: str
        Name of the file to read

        catalogue: astropy.table.Table
        The quasar catalogue fragment associated with this file

        Returns:
        ---------
        forests_by_targetid: dict
        Dictionary were forests are stored.

        Raise
        -----
        DataError if the analysis type is PK 1D and resolution data is not present
        """
        try:
            hdul = fitsio.FITS(filename)
        except IOError:
            self.logger.warning(f"Error reading '{filename}'. Ignoring file")
            return {}
        # Read targetid from fibermap to match to catalogue later
        fibermap = hdul['FIBERMAP'].read()
        targetid_spec = fibermap["TARGETID"]
        # First read all wavelength, flux, ivar, mask, and resolution
        # from this file
        spectrographs_data = {}
        colors = ["B", "R"]
        if "Z_FLUX" in hdul:
            colors.append("Z")
        else:
            self.logger.warning(f"Missing Z band from {filename}. Ignoring color.")

        reso_from_truth = False
        for color in colors:
            spec = {}
            try:
                spec["WAVELENGTH"] = hdul[f"{color}_WAVELENGTH"].read()
                spec["FLUX"] = hdul[f"{color}_FLUX"].read()
                spec["IVAR"] = (hdul[f"{color}_IVAR"].read() *
                                (hdul[f"{color}_MASK"].read() == 0))
                w = np.isnan(spec["FLUX"]) | np.isnan(spec["IVAR"])
                for key in ["FLUX", "IVAR"]:
                    spec[key][w] = 0.
                if self.analysis_type == "PK 1D":
                    if f"{color}_RESOLUTION" in hdul:
                        spec["RESO"] = hdul[f"{color}_RESOLUTION"].read()
                    else:
                        basename_truth=os.path.basename(filename).replace('spectra-','truth-')
                        pathname_truth=os.path.dirname(filename)
                        filename_truth=f"{pathname_truth}/{basename_truth}"
                        if os.path.exists(filename_truth):
                            if not reso_from_truth:
                                self.logger.debug("no resolution in files, reading from truth files")
                            reso_from_truth=True
                            with fitsio.FITS(filename_truth) as hdul_truth:
                                spec["RESO"] = hdul_truth[f"{color}_RESOLUTION"].read()
                        else:
                            raise DataError(
                                f"Error while reading {color} band from "
                                f"{filename}. Analysis type is 'PK 1D', "
                                "but file does not contain HDU "
                                f"'{color}_RESOLUTION'")
                spectrographs_data[color] = spec
            except OSError:
                self.logger.warning(
                    f"Error while reading {color} band from {filename}. "
                    "Ignoring color.")
        hdul.close()

        forests_by_targetid, num_data = self.format_data(
            catalogue,
            spectrographs_data,
            fibermap["TARGETID"],
            reso_from_truth=reso_from_truth)

        return forests_by_targetid

    def __call__(self, X):
        filename, catalogue = X

        return self.read_file(filename, catalogue)
