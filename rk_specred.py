#!/usr/bin/env python


"""
SPECREDUCE

General data reduction script for SALT long slit data.

This includes step that are not yet included in the pipeline 
and can be used for extended reductions of SALT data. 

It does require the pysalt package to be installed 
and up to date.

"""

import os
import sys
import glob
import shutil
import time

import matplotlib

import numpy
# import pyfits
# import astropy.io.fits as pyfits
from astropy.io import fits

from scipy.ndimage.filters import median_filter
import bottleneck
import scipy.interpolate

# Disable nasty and useless RankWarning when spline fitting
import warnings

# sys.path.insert(1, "/work/pysalt/")
# sys.path.insert(1, "/work/pysalt/plugins")
# sys.path.insert(1, "/work/pysalt/proptools")
# sys.path.insert(1, "/work/pysalt/saltfirst")
# sys.path.insert(1, "/work/pysalt/saltfp")
# sys.path.insert(1, "/work/pysalt/salthrs")
# sys.path.insert(1, "/work/pysalt/saltred")
# sys.path.insert(1, "/work/pysalt/saltspec")
# sys.path.insert(1, "/work/pysalt/slottools")
# sys.path.insert(1, "/work/pysalt/lib")

# from pyraf import iraf
# from iraf import pysalt

import pysalt
from pysalt.saltred.saltobslog import obslog
from pysalt.saltred.saltprepare import saltprepare
from pysalt.saltred.saltbias import saltbias
from pysalt.saltred.saltgain import saltgain
from pysalt.saltred.saltxtalk import saltxtalk
from pysalt.saltred.saltcrclean import saltcrclean
from pysalt.saltred.saltcombine import saltcombine
from pysalt.saltred.saltflat import saltflat
from pysalt.saltred.saltmosaic import saltmosaic
from pysalt.saltred.saltillum import saltillum

from pysalt.saltspec.specidentify import specidentify
from pysalt.saltspec.specrectify import specrectify
from pysalt.saltspec.specsky import skysubtract
from pysalt.saltspec.specextract import extract, write_extract
from pysalt.saltspec.specsens import specsens
from pysalt.saltspec.speccal import speccal

from PySpectrograph.Spectra import findobj

# import fits
import pysalt.mp_logging
import logging
import numpy
import pickle

from optparse import OptionParser

#
# Ralf Kotulla modules
#
from helpers import *
import wlcal
import traceline
import skysub2d
import optimal_spline_basepoints as optimalskysub
import skyline_intensity
import prep_science
import podi_cython
import optscale
import fiddle_slitflat2
import wlmodel
# this is just temporary to make debugging easier
import spline_pickle_test
import test_mask_out_obscured as find_obscured_regions
import map_distortions
import model_distortions
import find_sources
import zero_background
import tracespec
import optimal_extraction
import plot_high_res_sky_spec
import findcentersymmetry
import rectify_fullspec

matplotlib.use('Agg')
numpy.seterr(divide='ignore', invalid='ignore')
warnings.simplefilter('ignore', numpy.RankWarning)
# warnings.simplefilter('ignore', fits.fitsDeprecationWarning)
from astropy.utils.exceptions import *
warnings.simplefilter('ignore', AstropyDeprecationWarning)
warnings.simplefilter('ignore', FutureWarning)
warnings.simplefilter('ignore', UserWarning)


wlmap_fitorder = [2, 2]


def find_appropriate_arc(hdulist, arcfilelist, arcinfos=None,
                         accept_closest=False):
    if arcinfos is None:
        arcinfos = {}
    hdrs_to_match = [
        'CCDSUM',
        'WP-STATE',  # Waveplate State Machine State
        'ET-STATE',  # Etalon State Machine State
        'GR-STATE',  # Grating State Machine State
        'GR-STA',  # Commanded Grating Station
        'BS-STATE',  # Beamsplitter State Machine State
        'FI-STATE',  # Filter State Machine State
        'AR-STATE',  # Articulation State Machine State
        'AR-STA',  # Commanded Articulation Station
        'CAMANG',  # Commanded Articulation Station
        'POLCONF',  # Polarization configuration
        'GRATING',  # Commanded grating station
    ]
    flexmatch_headers = []
    if (not accept_closest):
        hdrs_to_match.append(
            'GRTILT',  # Commanded grating angle
        )
    else:
        flexmatch_headers.append(
            'GRTILT',  # Commanded grating angle
        )

    logger = logging.getLogger("FindGoodArc")
    logger.debug("Checking the following list of ARCs:\n * %s" % ("\n * ".join(arcfilelist)))

    matching_arcs = []
    for arcfile in arcfilelist:
        if (arcfile in arcinfos):
            # use header that we extracted in an earlier run
            hdr = arcinfos[arcfile]
        else:
            # this is a new file we haven't scanned before
            arc_hdulist = fits.open(arcfile)
            hdr = arc_hdulist[0].header
            arcinfos[arcfile] = hdr
            arc_hdulist.close()

        #
        # Now search for files with the identical spectral setup
        #
        matches = True
        for hdrname in hdrs_to_match:
            logger.debug("Comparing header key --> %s <--" % (hdrname))

            # if we can't compare the headers we'll assume they won't match
            if (not hdrname in hdulist[0].header or not hdrname in hdr):
                matches = False
                logger.debug("(%s) Not found in one of the two files" % (hdrname))
                break

            if (not hdulist[0].header[hdrname] == hdr[hdrname]):
                matches = False
                logger.debug("(%s) Found in both, but does not match!" % (hdrname))
                break

        # if all headers exist in both files and all headers match, 
        # then this ARC file should be usable to calibrate the OBJECT frame
        if (matches):
            logger.debug("FOUND GOOD ARC")
            matching_arcs.append(arcfile)

    #
    # If accepting closest matches as well, check for best match
    #
    flex_matches = []
    if (not accept_closest):
        return matching_arcs, True

    else:
        logger.info("Selecting the closest matched ARC from list")
        for arcfile in matching_arcs:
            if (arcfile in arcinfos):
                # use header that we extracted in an earlier run
                hdr = arcinfos[arcfile]
            else:
                # this is a new file we haven't scanned before
                arc_hdulist = fits.open(arcfile)
                hdr = arc_hdulist[0].header
                arcinfos[arcfile] = hdr
                arc_hdulist.close()

            fm = []
            for hdrname in flexmatch_headers:
                if (hdrname not in hdr):
                    fm.append(numpy.NaN)
                else:
                    fm.append(hdr[hdrname])
            flex_matches.append(fm)
        flex_matches = numpy.array(flex_matches)

        # target value
        target_fm = []
        for hdrname in flexmatch_headers:
            if (hdrname not in hdulist[0].header):
                target_fm.append(numpy.NaN)
            else:
                target_fm.append(hdulist[0].header[hdrname])
        target_fm = numpy.array(target_fm)

        diff = numpy.fabs((flex_matches - target_fm))
        closest = numpy.argmin(diff)

        #print matching_arcs
        #print closest
        #print numpy.array(matching_arcs)[closest]
        matching_arcs = [numpy.array(matching_arcs)[closest]]

        return matching_arcs, diff[closest]==0
    # print "***\n" * 3, matching_arcs, "\n***" * 3



def tiledata(hdulist, rssgeom):
    logger = logging.getLogger("TileData")

    out_hdus = [hdulist[0]]

    gap, xshift, yshift, rotation = rssgeom
    xshift = numpy.array(xshift)
    yshift = numpy.array(yshift)

    # Gather information about existing extensions
    sci_exts = []
    var_exts = []
    bpm_exts = []
    detsecs = []

    exts = {}  # 'SCI': [], 'VAR': [], 'BPM': [] }
    ext_order = ['SCI', 'BPM', 'VAR']
    for e in ext_order:
        exts[e] = []

    for i in range(1, len(hdulist)):
        if (hdulist[i].header['EXTNAME'] == 'SCI'):

            # Remember this function for later use
            sci_exts.append(i)
            exts['SCI'].append(i)

            # Also find out the detsec header entry so we can put all chips 
            # in the right order (blue to red) without having to rely on 
            # ordering within the file
            decsec = hdulist[i].header['DETSEC']
            detsec_startx = int(decsec[1:-1].split(":")[0])
            detsecs.append(detsec_startx)

            var_ext, bpm_ext = -1, -1
            if ('VAREXT' in hdulist[i].header):
                var_ext = hdulist[i].header['VAREXT']
            if ('BPMEXT' in hdulist[i].header):
                bpm_ext = hdulist[i].header['BPMEXT']
            var_exts.append(var_ext)
            bpm_exts.append(bpm_ext)

            exts['VAR'].append(var_ext)
            exts['BPM'].append(bpm_ext)

    # print sci_exts
    # print detsecs

    #
    # Better make sure we have all 6 CCDs
    #
    # Problem: How to handle different readout windows here???
    #
    if (len(sci_exts) != 6):
        logger.critical("Could not find all 6 CCD sections!")
        return

    # convert to numpy array
    detsecs = numpy.array(detsecs)
    sci_exts = numpy.array(sci_exts)

    # sort extensions by DETSEC position
    detsec_sort = numpy.argsort(detsecs)
    sci_exts = sci_exts[detsec_sort]

    for name in exts:
        exts[name] = numpy.array(exts[name])[detsec_sort]

    # print exts

    #
    # Now we have all extensions in the right order
    #

    # Compute how big the output array should be
    width = 0
    height = -1
    amp_width = numpy.zeros_like(sci_exts)
    amp_height = numpy.zeros_like(sci_exts)
    for i, ext in enumerate(sci_exts):
        amp_width[i] = hdulist[ext].data.shape[1]
        amp_height[i] = hdulist[ext].data.shape[0]

    # Add in the widths of all gaps
    binx, biny = pysalt.get_binning(hdulist)
    logger.debug("Creating tiled image using binning %d x %d" % (binx, biny))

    width = numpy.sum(amp_width) + 2 * gap / binx  # + numpy.sum(numpy.fabs((xshift/binx).round()))
    height = numpy.max(amp_height)  # + numpy.sum(numpy.fabs((yshift/biny).round()))

    # print width, height
    # print xshift
    # print yshift

    for name in ext_order:

        logger.debug("Starting tiling for extension %s !" % (name))

        # Now create the mosaics
        data = numpy.empty((height, width))
        data[:, :] = numpy.NaN

        for i, ext in enumerate(exts[name]):  # sci_exts):

            dx_gaps = int(gap * int(i / 2) / binx)
            dx_shift = xshift[int(i / 2)] / binx
            startx = numpy.sum(amp_width[0:i])

            # Add in gaps if applicable
            startx += dx_gaps
            # Also factor in the small corrections
            # startx -= dx_shift

            endx = startx + amp_width[i]

            logger.debug("Putting extension %d (%s) at X=%d -- %d (gaps=%d, shift=%d)" % (
                i, name, startx, endx, dx_gaps, dx_shift))
            # logger.info("input size: %d x %d" % (amp_width[i], amp_height[i]))
            # logger.info("output size: %d x %d" % (amp_width[i], height))
            data[:, startx:endx] = hdulist[ext].data[:, :amp_width[i]]

        imghdu = fits.ImageHDU(data=data)
        imghdu.name = name
        out_hdus.append(imghdu)

    logger.debug("Finished tiling for all %d data products" % (len(ext_order)))

    return fits.HDUList(out_hdus)


def salt_prepdata(infile, badpixelimage=None, create_variance=False,
                  masterbias=None, clean_cosmics=True,
                  flatfield_frame=None, mosaic=False,
                  verbose=False, *args):
    _, fb = os.path.split(infile)
    logger = logging.getLogger("PrepData(%s)" % (fb))
    logger.info("Working on file %s" % (infile))

    # hdulist = fits.open(infile)
    hdulist = fits.open(infile)
    # print hdulist, type(hdulist)

    pysalt_log = None  # 'pysalt.log'

    badpixel_hdu = None
    if (not badpixelimage is None):
        badpixel_hdu = fits.open(badpixelimage)

    #
    # Do some prepping
    #
    # hdulist.info()

    logger.debug("Prepare'ing")
    hdulist = pysalt.saltred.saltprepare.prepare(
        hdulist,
        createvar=create_variance,
        badpixelstruct=badpixel_hdu)
    # Add some history headers here

    #
    # Overscan/bias
    #
    logger.debug("Subtracting bias & overscan")
    # for ext in hdulist:
    #     if (not ext.data == None): print ext.data.shape
    bias_hdu = None
    if (not masterbias is None and os.path.isfile(masterbias)):
        bias_hdu = fits.open(masterbias)
    hdulist = pysalt.saltred.saltbias.bias(
        hdulist,
        subover=True, trim=True, subbias=False,
        bstruct=bias_hdu,
        median=False, function='polynomial', order=5, rej_lo=3.0, rej_hi=5.0,
        niter=10, plotover=False,
        log=pysalt_log, verbose=verbose)
    logger.debug("done with bias & overscan")

    # print "--------------"
    # for ext in hdulist:
    #     if (not ext.data == None): print ext.data.shape

    # Again, add some headers here

    #
    # Gain
    #
    logger.debug("Correcting gain")
    dblist = []  # saltio.readgaindb(gaindb)
    hdulist = pysalt.saltred.saltgain.gain(hdulist,
                                           mult=True,
                                           usedb=False,
                                           dblist=dblist,
                                           log=pysalt_log, verbose=verbose)
    logger.debug("done with gain")

    #
    # Xtalk
    #
    logger.debug("fixing crosstalk")
    usedb = False
    if usedb:
        xtalkfile = xtalkfile.strip()
        xdict = saltio.readxtalkcoeff(xtalkfile)
    else:
        xdict = None
    if usedb:
        obsdate = saltkey.get('DATE-OBS', struct[0])
        obsdate = int('%s%s%s' % (obsdate[0:4], obsdate[5:7], obsdate[8:]))
        xkey = numpy.array(xdict.keys())
        date = xkey[abs(xkey - obsdate).argmin()]
        xcoeff = xdict[date]
    else:
        xcoeff = []

    hdulist = pysalt.saltred.saltxtalk.xtalk(hdulist, xcoeff, log=pysalt_log, verbose=verbose)
    logger.debug("done with crosstalk")

    #
    # crj-clean
    #
    # clean the cosmic rays
    multithread = True
    logger.debug("removing cosmics")
    if multithread and len(hdulist) > 1:
        crj_function = pysalt.saltred.saltcrclean.multicrclean
    else:
        crj_function = pysalt.saltred.saltcrclean.crclean
    if (clean_cosmics):
        # hdulist = crj_function(hdulist,
        #                        crtype='edge', thresh=5, mbox=11, bthresh=5.0,
        #                        flux_ratio=0.2, bbox=25, gain=1.0, rdnoise=5.0, fthresh=5.0, bfactor=2,
        #                        gbox=3, maxiter=5)


        sigclip = 5.0
        sigfrac = 0.6
        objlim = 5.0
        saturation_limit = 65000

        # This is BEFORE mosaicing, therefore:
        # Loop over all SCI extensions
        for ext in hdulist:
            if (ext.name == 'SCI'):
                with open("headers", "a") as h:
                    print >> h, ext.header

                gain = 1.5 if (not 'GAIN' in ext.header) else ext.header['GAIN']
                readnoise = 3 if (not 'RDNOISE' in ext.header) else ext.header['RDNOISE']

                crj = podi_cython.lacosmics(
                    ext.data.astype(numpy.float64),
                    gain=gain,
                    readnoise=readnoise,
                    niter=3,
                    sigclip=sigclip, sigfrac=sigfrac, objlim=objlim,
                    saturation_limit=saturation_limit,
                    verbose=False
                )
                cell_cleaned, cell_mask, cell_saturated = crj
                ext.data = cell_cleaned

    logger.debug("done with cosmics")

    #
    # Apply flat-field correction if requested
    #
    logger.info("FLAT: %s" % (str(flatfield_frame)))
    if (not flatfield_frame is None and os.path.isfile(flatfield_frame)):
        logger.debug("Applying flatfield")
        flathdu = fits.open(flatfield_frame)
        pysalt.saltred.saltflat.flat(
            struct=hdulist,  # input
            fstruct=flathdu,  # flatfield
        )
        # saltflat('xgbpP*fits', '', 'f', flatimage, minflat=500, clobber=True, logfile=logfile, verbose=True)
        flathdu.close()
        logger.debug("done with flatfield")
    else:
        logger.debug("continuing without flat-field correction!")

    if (mosaic):
        logger.debug("Mosaicing all chips together")
        geomfile = pysalt.get_data_filename("pysalt$data/rss/RSSgeom.dat")
        geomfile = pysalt.get_data_filename("data/rss/RSSgeom.dat")
        logger.debug("Reading geometry from file %s (%s)" % (geomfile, os.path.isfile(geomfile)))

        # does CCD geometry definition file exist
        if (not os.path.isfile(geomfile)):
            logger.critical("Unable to read geometry file %s!" % (geomfile))
        else:

            gap = 0
            xshift = [0, 0]
            yshift = [0, 0]
            rotation = [0, 0]
            gap, xshift, yshift, rotation, status = pysalt.lib.saltio.readccdgeom(geomfile, logfile=None, status=0)
            logger.debug("Using CCD geometry: gap=%d, Xshift=%d,%d, Yshift=%d,%d, rot=%d,%d" % (
                gap, xshift[0], xshift[1], yshift[0], yshift[1], rotation[0], rotation[1]))
            # print "\n@@"*5, gap, xshift, yshift, rotation, "\n@"*5

            logger.debug("mosaicing -- GAP:%f - X-shift:%f/%f  y-shift:%f/%f  rotation:%f/%f" % (
                gap, xshift[0], xshift[1], yshift[0], yshift[1], rotation[0], rotation[1]))

            # logger.info("File structure before mosaicing:")
            # hdulist.info()

            gap = 90
            xshift = [0.0, +5.9, -2.1]
            yshift = [0.0, -2.6, 0.4]
            rotation = [0, 0, 0]
            hdulist = tiledata(hdulist, (gap, xshift, yshift, rotation))
            # #return

            # create the mosaic
            # logger.info("Running IRAF geotran to create mosaic, be patient!")
            # hdulist = pysalt.saltred.saltmosaic.make_mosaic(
            #     struct=hdulist, 
            #     gap=gap, xshift=xshift, yshift=yshift, rotation=rotation, 
            #     interp_type='linear',              
            #     boundary='constant', constant=0, geotran=True, fill=False,
            #     #boundary='constant', constant=0, geotran=False, fill=False,
            #     cleanup=True, log=None, verbose=verbose)
            # hdulist[2].name = 'VAR'
            # hdulist[3].name = 'BPM'
            # hdulist.info()
            # logger.debug("done with mosaic")

    return hdulist


def save_sky_spec(wl, sky_spline, hi_res=None):

    wl_min = numpy.min(wl)
    wl_max = numpy.max(wl)
    mean_dwl = (wl_max - wl_min) / wl.shape[1]

    if (hi_res is None):
        hi_res = 0.1 * mean_dwl

    n_points = int((wl_max - wl_min) / hi_res)

    sky_wl = numpy.arange(n_points, dtype=numpy.float) * hi_res + wl_min
    sky_flux = sky_spline(sky_wl)

    imghdu = fits.ImageHDU(
        data=sky_flux,
        name="SKYSPEC"
    )

    imghdu.header['WCSNAME'] = "calibrated wavelength"
    imghdu.header['CRPIX1'] = 1.
    imghdu.header['CRVAL1'] = wl_min
    imghdu.header['CD1_1'] = hi_res
    imghdu.header['CTYPE1'] = "AWAV"
    imghdu.header['CUNIT1'] = "Angstrom"

    return imghdu





#################################################################################
#################################################################################
#################################################################################

def specred(rawdir, prodir, options,
            imreduce=True, specreduce=True,
            calfile=None, lamp='Ar',
            automethod='Matchlines', skysection=[800, 1000],
            cleanup=True):
    #print rawdir
    #print prodir

    logger = logging.getLogger("SPECRED")

    # get the name of the files
    # if (type(infile) == list):
    #     infile_list = infile
    # elif (type(infile) == str and os.path.isdir(infile)):
    infile_list = glob.glob(os.path.join(rawdir, "*.fits"))

    # get the current date for the files
    obsdate = os.path.basename(infile_list[0])[1:9]
    #print obsdate

    # set up some files that will be needed
    logfile = 'spec' + obsdate + '.log'
    flatimage = 'FLAT%s.fits' % (obsdate)
    dbfile = 'spec%s.db' % obsdate

    # create the observation log
    # obs_dict=obslog(infile_list)

    # import pysalt.lib.saltsafeio as saltio

    #print infile_list

    #
    #
    # Now reduce all files, one by one
    #
    #
    # work_dir = "working/"
    # if (not os.path.isdir(work_dir)):
    #     os.mkdir(work_dir)

    # #
    # # Make sure we have all directories 
    # #
    # for rs in reduction_steps:
    #     dirname = "%s/%s" % (work_dir, rs)
    #     if (not os.path.isdir(dirname)):
    #         os.mkdir(dirname)

    #
    # Go through the list of files, find out what type of file they are
    #
    logger.info("Identifying frames and sorting by type (object/flat/arc)")
    obslog = {
        'FLAT':   [],
        'ARC':    [],
        'OBJECT': [],
    }

    for idx, filename in enumerate(infile_list):
        hdulist = fits.open(filename)
        if (not hdulist[0].header['INSTRUME'] == "RSS"):
            logger.info("Frame %s is not a valid RSS frame (instrument: %s)" % (
                filename, hdulist[0].header['INSTRUME']))
            continue

        obstype = None
        if ('OBSTYPE' in hdulist[0].header):
            obstype = hdulist[0].header['OBSTYPE']
            if (obstype not in ['OBJECT', 'ARC', 'FLAT']):
                obstype = None
        if (obstype is None or (obstype.strip() == "" and 'CCDTYPE' in hdulist[0].header)):
            obstype = hdulist[0].header['CCDTYPE']
        if (obstype in obslog):
            obslog[obstype].append(filename)
            logger.debug("Identifying %s as %s" % (filename, obstype))
        else:
            logger.info("No idea what to do with frame %s --> %s" % (filename, obstype))

    for obstype in obslog:
        if (len(obslog[obstype]) > 0):
            logger.info("Found the following %ss:\n -- %s" % (
                obstype, "\n -- ".join(obslog[obstype])))
        else:
            logger.info("No files of type %s found!" % (obstype))

    if (options.check_only):
        return

    #
    # Go through the list of files, find all flat-fields, and create a master flat field
    #
    logger.info("Creating a master flat-field frame")
    flatfield_filenames = []
    flatfield_hdus = {}
    first_flat = None
    flatfield_list = {}

    for idx, filename in enumerate(obslog['FLAT']):
        hdulist = fits.open(filename)
        obstype = None
        if ('OBSTYPE' in hdulist[0].header):
            obstype = hdulist[0].header['OBSTYPE']
        if (obstype is None or (obstype.strip() == "" and 'CCDTYPE' in hdulist[0].header)):
            obstype = hdulist[0].header['CCDTYPE']
        if (obstype.find("FLAT") >= 0 and
            hdulist[0].header['INSTRUME'] == "RSS" and
            options.use_flats):
            #
            # This is a flat-field
            #

            #
            # Get some parameters so we can create flatfields for each specific
            # instrument configuration
            #
            grating = hdulist[0].header['GRATING']
            grating_angle = hdulist[0].header['GR-ANGLE']
            grating_tilt = hdulist[0].header['GRTILT']
            binning = "x".join(hdulist[0].header['CCDSUM'].split())

            if (not grating in flatfield_list):
                flatfield_list[grating] = {}
            if (not binning in flatfield_list[grating]):
                flatfield_list[grating][binning] = {}
            if (not grating_tilt in flatfield_list[grating][binning]):
                flatfield_list[grating][binning][grating_tilt] = {}
            if (not grating_angle in flatfield_list[grating][binning][grating_tilt]):
                flatfield_list[grating][binning][grating_tilt][grating_angle] = []

            flatfield_list[grating][binning][grating_tilt][grating_angle].append(filename)

    for grating in flatfield_list:
        for binning in flatfield_list[grating]:
            for grating_tilt in flatfield_list[grating][binning]:
                for grating_angle in flatfield_list[grating][binning][grating_tilt]:

                    filelist = flatfield_list[grating][binning][grating_tilt][grating_angle]
                    flatfield_hdus = {}

                    logger.info("Creating master flatfield for %s (%.3f/%.3f), %s (%d frames)" % (
                        grating, grating_angle, grating_tilt, binning, len(filelist)))

                    for filename in filelist:

                        _, fb = os.path.split(filename)
                        single_flat = "flat_%s" % (fb)

                        hdu = salt_prepdata(filename,
                                            badpixelimage=None,
                                            create_variance=True,
                                            clean_cosmics=False,
                                            mosaic=False,
                                            verbose=False)
                        pysalt.clobberfile(single_flat)
                        hdu.writeto(single_flat, clobber=True)
                        logger.info("Wrote single flatfield to %s" % (single_flat))

                        for extid, ext in enumerate(hdu):
                            if (ext.name == "SCI"):
                                # Only use the science extensions, leave everything else 
                                # untouched: Apply a one-dimensional median filter to take 
                                # out spectral slope. We can then divide the raw data by this 
                                # median flat to isolate pixel-by-pixel variations
                                filtered = scipy.ndimage.filters.median_filter(
                                    input=ext.data,
                                    size=(1, 25),
                                    footprint=None,
                                    output=None,
                                    mode='reflect',
                                    cval=0.0,
                                    origin=0)
                                ext.data /= filtered

                                if (not extid in flatfield_hdus):
                                    flatfield_hdus[extid] = []
                                flatfield_hdus[extid].append(ext.data)

                        single_flat = "norm" + single_flat
                        pysalt.clobberfile(single_flat)
                        hdu.writeto(single_flat, clobber=True)
                        logger.info("Wrote normalized flatfield to %s" % (single_flat))

                        if (first_flat is None):
                            first_flat = hdulist

                    print first_flat

                    if (len(filelist) <= 0):
                        continue

                    # Combine all flat-fields into a single master-flat
                    for extid in flatfield_hdus:
                        flatstack = flatfield_hdus[extid]
                        # print "EXT",extid,"-->",flatstack
                        logger.info("Ext %d: %d flats" % (extid, len(flatstack)))
                        flatstack = numpy.array(flatstack)
                        print flatstack.shape
                        avg_flat = numpy.mean(flatstack, axis=0)
                        print "avg:", avg_flat.shape

                        first_flat[extid].data = avg_flat

                    masterflat_filename = "flat__%s_%s_%.3f_%.3f.fits" % (
                        grating, binning, grating_tilt, grating_angle)
                    pysalt.clobberfile(masterflat_filename)
                    first_flat.writeto(masterflat_filename, clobber=True)

                    # # hdu = salt_prepdata(filename, badpixelimage=None, create_variance=False,
                    # #                     verbose=False)
                    # # flatfield_hdus.append(hdu)

    #############################################################################
    #
    # Determine a wavelength solution from ARC frames, where available
    #
    #############################################################################

    logger.info("Searching for a wavelength calibration from the ARC files")
    skip_wavelength_cal_search = False  # os.path.isfile(dbfile)

    # Keep track of when the ARCs were taken, so we can pick the one closest 
    # in time to the science observation for data reduction
    arc_obstimes = numpy.ones((len(obslog['ARC']))) * -999.9
    arc_mosaic_list = [None] * len(obslog['ARC'])
    arc_mef_list = [None] * len(obslog['ARC'])
    if (not skip_wavelength_cal_search):
        for idx, filename in enumerate(obslog['ARC']):
            _, fb = os.path.split(filename)
            hdulist = fits.open(filename)

            # Use Julian Date for simple time indexing
            arc_obstimes[idx] = hdulist[0].header['JD']

            arc_filename = "ARC_%s" % (fb)
            arc_mosaic_filename = "ARC_m_%s" % (fb)
            rect_filename = "ARC-RECT_%s" % (fb)

            if (os.path.isfile(arc_mosaic_filename) and options.reusearcs):
                arc_mosaic_list[idx] = arc_mosaic_filename
                logger.info("Re-using ARC %s from previous run" % (arc_mosaic_filename))
                continue

            logger.info("Creating MEF  for frame %s --> %s" % (fb, arc_filename))
            hdu = salt_prepdata(filename,
                                badpixelimage=None,
                                create_variance=True,
                                clean_cosmics=False,
                                mosaic=False,
                                verbose=False)
            pysalt.clobberfile(arc_filename)
            hdu.writeto(arc_filename, clobber=True)
            arc_mef_list[idx] = arc_filename

            logger.info("Creating mosaic for frame %s --> %s" % (fb, arc_mosaic_filename))
            hdu_mosaiced = salt_prepdata(filename,
                                         badpixelimage=None,
                                         create_variance=True,
                                         clean_cosmics=False,
                                         mosaic=True,
                                         verbose=False)

            #
            # Now we have a HDUList of the mosaiced ARC file, so 
            # we can continue to the wavelength calibration
            #
            logger.info("Starting wavelength calibration")
            binx, biny = pysalt.get_binning(hdulist)

            logger.info("Checking symmetry of ARC lines to tune the spectropgraph model")
            symmetry_lines, best_midline, linewidth = \
                findcentersymmetry.find_curvature_symmetry_line(
                    hdulist=hdu_mosaiced,
                    data_ext='SCI',
                    avg_width=10,
                    n_lines=10,
            )
            reference_row = int(best_midline[1])
            logger.info("Using row %d as reference row" % (reference_row))
            hdu_mosaiced[0].header['WLREFROW'] = (
                reference_row, "symmetry row")
            hdu_mosaiced[0].header['WLREFCOL'] = (
                best_midline[0], "approx line position x")
            hdu_mosaiced[0].header['LINEWDTH'] = (
                linewidth, "linewidth in pixels")

            wls_data = wlcal.find_wavelength_solution(
                hdu_mosaiced,
                line=reference_row,
                #line=(2070/biny)
            )
            if (wls_data is None):
                logger.error("Unable to compute WL map from %s" % (filename))
                continue

            #
            # Write wavelength solution to FITS header so we can access it 
            # again if we need to at a later point
            #
            logger.info("Storing wavelength solution in ARC file (%s)" % (arc_mosaic_filename))
            hdu_mosaiced[0].header['WLSFIT_N'] = len(wls_data['wl_fit_coeffs'])
            for i in range(len(wls_data['wl_fit_coeffs'])):
                hdu_mosaiced[0].header['WLSFIT_%d' % (i)] = wls_data['wl_fit_coeffs'][i]

            #
            # Now add some plotting here just to make sure the user is happy :-)
            #
            logger.info("Creating calibration plot for user")
            plotfile = arc_mosaic_filename[:-5] + ".png"
            wlcal.create_wl_calibration_plot(wls_data, hdu_mosaiced, plotfile)

            #
            # Simulate the ARC spectrum by extracting a 2-D ARC spectrum just 
            # like we would for the sky-subtraction in OBJECT frames
            #
            logger.info("Computing a 2-D wavelength solution by tracing arc lines")
            arc_region_file = "ARC_m_%s_traces.reg" % (fb[:-5])
            wls_2darc = traceline.compute_2d_wavelength_solution(
                arc_filename=hdu_mosaiced,
                n_lines_to_trace=-15,  # -50, # trace all lines with S/N > 50
                fit_order=wlmap_fitorder,
                output_wavelength_image="wl+image.fits",
                debug=True,
                arc_region_file=arc_region_file,
                trace_every=0.05,
                wls_data=wls_data,
            )
            wl_hdu = fits.ImageHDU(data=wls_2darc)
            wl_hdu.name = "WAVELENGTH"
            wl_hdu.header['OBJECT'] = ("wavelength map (ARC-trace)", "description")
            hdu_mosaiced.append(wl_hdu)

            #
            # Compute a synthetic 2-D wavelength model
            #
            logger.info("Computing 2-D wavelength map from RSS spectrograph model")
            model_wl = wlmodel.rssmodelwave(
                header=hdu_mosaiced[0].header,
                img=hdu_mosaiced['SCI'].data,
                xbin=binx, ybin=biny,
                y_center=reference_row*biny,
            )
            hdu_mosaiced.append(
                fits.ImageHDU(
                    data=model_wl,
                    name="WL_MODEL_2D",
                    header=fits.Header(
                        {"OBJECT": "wavelength map from RSS model"}
                    )
                )
            )
            fits.PrimaryHDU(data=model_wl).writeto("arcwl.fits", clobber=True)
            hdu_mosaiced[0].header['RSSYCNTR'] = (
                reference_row*biny,
                "reference line for spectrograph model"
            )


            #
            # Now go ahead and extract the full 2-d sky
            #
            logger.info("Extracting a ARC-spectrum from the entire frame")
            arc_regions = numpy.array([[0, hdu_mosaiced['SCI'].data.shape[0]]])
            hdu_mosaiced.writeto("dummy.fits", clobber=True)
            arc2d = skysub2d.make_2d_skyspectrum(
                hdu_mosaiced,
                model_wl, #wls_2darc,
                sky_regions=arc_regions,
                oversample_factor=1.0,
            )
            simul_arc_hdu = fits.ImageHDU(data=arc2d)
            simul_arc_hdu.name = "SIMULATION"
            hdu_mosaiced.append(simul_arc_hdu)

            logger.info("Writing calibrated ARC frame to file (%s)" % (arc_mosaic_filename))
            pysalt.clobberfile(arc_mosaic_filename)
            hdu_mosaiced.writeto(arc_mosaic_filename, clobber=True)
            arc_mosaic_list[idx] = arc_mosaic_filename


            # lamp=hdu[0].header['LAMPID'].strip().replace(' ', '')
            # lampfile=pysalt.get_data_filename("pysalt$data/linelists/%s.txt" % lamp)
            # automethod='Matchlines'
            # skysection=[800,1000]
            # logger.info("Searching for wavelength solution (lamp:%s, arc-image:%s)" % (
            #     lamp, arc_filename))
            # specidentify(arc_filename, lampfile, dbfile, guesstype='rss', 
            #              guessfile='', automethod=automethod,  function='legendre',  order=5, 
            #              rstep=100, rstart='middlerow', mdiff=10, thresh=3, niter=5, 
            #              inter=False, clobber=True, logfile=logfile, verbose=True)
            # logger.debug("Done with specidentify")

            # logger.debug("Starting specrectify")
            # specrectify(arc_filename, outimages=rect_filename, outpref='',
            #             solfile=dbfile, caltype='line', 
            #             function='legendre',  order=3, inttype='interp', 
            #             w1=None, w2=None, dw=None, nw=None,
            #             blank=0.0, clobber=True, logfile=logfile, verbose=True)

            # logger.debug("Done with specrectify")

    if (options.arc_only):
        logger.info("Only ARCs were requested, all done!")
        return
    if (arc_mosaic_list is None or len(arc_mosaic_list) <= 0):
        logger.error("NO VALID ARCs FOUND, aborting.")
        return

    # return
    # os._exit(0)

    # with open("flatlist", "w") as picklefile:
    #    pickle.dump(flatfield_list, picklefile)
    # print "\nPICKLE done"*10

    #############################################################################
    #
    # Now apply wavelength solution found above to your data frames
    #
    #############################################################################
    logger.info("\n\n\nProcessing OBJECT frames")
    arcinfos = {}
    for idx, filename in enumerate(obslog['OBJECT']):

        hdu_appends = []

        _, fb = os.path.split(filename)
        _fb, _ = os.path.splitext(fb)
        hdulist = fits.open(filename)
        logger = logging.getLogger("OBJ(%s)" % _fb)

        binx, biny = pysalt.get_binning(hdulist)
        logger.info("Using binning of %d x %d (spectral/spatial)" % (binx, biny))

        mosaic_filename = "OBJ_raw__%s" % (fb)
        output_basename = "OBJ_%s" % (fb[:-5])
        out_filename =  "%s.fits" % (output_basename)

        grating = hdulist[0].header['GRATING']
        grating_angle = hdulist[0].header['GR-ANGLE']
        grating_tilt = hdulist[0].header['GRTILT']
        binning = "x".join(hdulist[0].header['CCDSUM'].split())

        # Find the most appropriate flat-field
        if (grating in flatfield_list):
            if (binning in flatfield_list[grating]):
                if (grating_tilt in flatfield_list[grating][binning]):
                    _grating_tilt = grating_tilt
                else:
                    # We can handle flatfields with non-matching grating-tilts
                    # make sure to pick the closest one
                    grating_tilts = numpy.array(flatfield_list[grating][binning].keys())
                    closest = numpy.argmin(numpy.fabs(grating_tilts - grating_tilt))
                    _grating_tilt = grating_tilts[closest]

                if (grating_angle in flatfield_list[grating][binning][_grating_tilt]):
                    _grating_angle = grating_angle
                else:
                    grating_angles = numpy.array(flatfield_list[grating][binning][_grating_tilt].keys())
                    closest = numpy.argmin(numpy.fabs(grating_angles - grating_angle))
                    _grating_angle = grating_angles[closest]

                masterflat_filename = "flat__%s_%s_%.3f_%.3f.fits" % (
                    grating, binning, _grating_angle, _grating_tilt)

            else:
                masterflat_filename = None
        else:
            masterflat_filename = None

        masterflat_filename = None

        logger.info("FLATX: %s (%s, %f, %f, %s) = %s" % (
            str(masterflat_filename),
            grating, grating_angle, grating_tilt, binning,
            filename)
                    )
        if (not masterflat_filename is None):
            if (not os.path.isfile(masterflat_filename)):
                masterflat_filename = None

        #
        # Find the ARC closest in time to this frame
        #
        # obj_jd = hdulist[0].header['JD']
        # delta_jd = numpy.fabs(arc_obstimes - obj_jd)
        # good_arc_idx = numpy.argmin(delta_jd)
        # good_arc = arc_mosaic_list[good_arc_idx]
        # logger.info("Using ARC %s for wavelength calibration" % (good_arc))
        # good_arc_list = find_appropriate_arc(hdu, obslog['ARC'], arcinfos)
        raw_hdu = fits.open(filename)
        good_arc_list, exact_match = find_appropriate_arc(
            raw_hdu, arc_mosaic_list,
            arcinfos,
            accept_closest=options.use_closest_arc,
        )
        logger.debug("Found these ARCs as appropriate:\n -- %s" % ("\n -- ".join(good_arc_list)))

        if (len(good_arc_list) == 0):
            logger.error("Could not find any appropriate ARCs")
            continue
        elif (not exact_match):
            good_arc = good_arc_list[0]
            logger.warning("Couldn't find exact matching ARC, using closest match")
        else:
            good_arc = good_arc_list[0]
            logger.info("Using ARC %s for wavelength calibration" % (good_arc))

        # open the ARC frame
        arc_hdu = fits.open(good_arc)

        logger.info("Creating mosaic for frame %s --> %s" % (fb, mosaic_filename))
        hdu = salt_prepdata(filename,
                            flatfield_frame=masterflat_filename,
                            badpixelimage=None,
                            create_variance=True,
                            clean_cosmics=False,  # True,
                            mosaic=True,
                            verbose=False,
                            )
        pysalt.clobberfile(mosaic_filename)
        logger.info("Writing mosaiced OBJ file to %s" % (mosaic_filename))
        hdu.writeto(mosaic_filename, clobber=True)

        img_data = numpy.array(hdu['SCI'].data)

        #
        # Find bad rows that are not well exposed and likely contain no useful information
        #
        bad_rows = find_obscured_regions.find_obscured_regions(img_data)
        img_data[bad_rows, :] = numpy.NaN

        #
        # Save the bad-column data as image extension in the output frame
        #
        bad_rows_img = numpy.zeros((img_data.shape[0]), dtype=numpy.int)
        bad_rows_img[bad_rows] = 1
        bad_rows_ext = fits.ImageHDU(data=bad_rows_img, name="BADROWS")
        hdu_appends.append(bad_rows_ext)

        #
        # Also create the image without cosmic ray rejection, and add it to the 
        # output file
        #
        logger.info("Creating mosaiced frame WITHOUT cosmic-ray rejection")
        hdulist_crj = salt_prepdata(filename,
                                    flatfield_frame=masterflat_filename,
                                    create_variance=True,
                                    badpixelimage=None,
                                    clean_cosmics=True,
                                    mosaic=True,
                                    verbose=False,
                                    )
        #hdu_sci_nocrj = hdu_nocrj['SCI']
        #hdu_sci_nocrj.name = 'SCI.NOCRJ'
        #hdu.append(hdu_sci_nocrj)
        hdu_crj = hdulist_crj['SCI']
        hdu_crj.name = 'SCI.CRJ'
        hdu.append(hdu_crj)
        img_crjclean = hdu_crj.data


        # Make backup of the image BEFORE sky subtraction
        # make sure to copy the actual data, not just create a duplicate reference
        # for source_ext in ['SCI', 'SCI.NOCRJ']:
        #     presub_hdu = fits.ImageHDU(data=numpy.array(hdu['SCI'].data),
        #                                header=hdu['SCI'].header)
        #     presub_hdu.name = source_ext + '.RAW'
        #     hdu.append(presub_hdu)

        #
        # Find symmetry from sky-lines
        #
        logger.info("Checking symmetry of SKY lines to tune the spectropgraph model")
        symmetry_lines, best_midline, linewidth = \
            findcentersymmetry.find_curvature_symmetry_line(
                hdulist=hdu,
                data_ext='SCI',
                avg_width=10,
                n_lines=10,
        )
        if (symmetry_lines is None):
            # This means we could not find any valid linetraces
            # assume the center from the corresponding arc
            logger.warning("Adopting symmetry row from ARC")
            reference_row = arc_hdu[0].header['WLREFROW']
            linewidth = arc_hdu[0].header['LINEWDTH']
        else:
            reference_row = int(best_midline[1])
            logger.info("Using row %d as reference row" % (reference_row))

        #
        # Find a global slit profile to identify obscured regions (i.e. behind guide and/or focus probe)
        #
        img_raw = img_data.copy()
        profile_raw_1d = numpy.mean(img_raw, axis=1)
        # print profile_raw_1d

        #
        # Use ARC to trace lines and compute a 2-D wavelength solution
        #
        logger.info("Computing 2-D wavelength map")
        arc_region_file = "OBJ_%s_traces.reg" % (fb[:-5])
        # wls_2d, slitprofile = traceline.compute_2d_wavelength_solution(
        #     arc_filename=good_arc, 
        #     n_lines_to_trace=-50, # trace all lines with S/N > 50 
        #     fit_order=wlmap_fitorder,
        #     output_wavelength_image="wl+image.fits",
        #     debug=False,
        #     arc_region_file=arc_region_file,
        #     return_slitprofile=True,
        #     trace_every=0.05)
        # print wls_2d
        # wl_hdu = fits.ImageHDU(data=wls_2d)
        # wl_hdu.name = "WAVELENGTH"
        # hdu.append(wl_hdu)

        # This uses the ARC tracing & polynomial fit WL solution
        wls_2d = arc_hdu['WAVELENGTH'].data

        # BETTER: Use the 2-D model fit as WL solution
        # This would also be saved in the ARC reference frame as WL_MODEL_2D
        model_wl = wlmodel.rssmodelwave(
            header=arc_hdu[0].header,
            img=arc_hdu['SCI'].data,
            xbin=binx, ybin=biny,
            y_center=reference_row * biny,
        )
        # wls_2d = arc_hdu['WL_MODEL_2D'].data
        wls_2d = model_wl

        fits.PrimaryHDU(data=wls_2d).writeto("specred.wl.fits", clobber=True)
        # os._exit(-1)

        n_params = arc_hdu[0].header['WLSFIT_N']
        # copy a couple of relevant keywords
        for key in ['RSSYCNTR', 'WLSFIT_N', 'LINEWDTH']:
            if (key in arc_hdu[0].header):
                hdu[0].header[key] = arc_hdu[0].header[key]
            else:
                logger.warning("Unable to find FITS keywords %s in %s" % (key, good_arc))
        # hdu[0].header["WLSFIT_N"] = arc_hdu[0].header["WLSFIT_N"]

        wls_fit = numpy.zeros(n_params)
        for i in range(n_params):
            wls_fit[i] = arc_hdu[0].header['WLSFIT_%d' % (i)]
            hdu[0].header['WLSFIT_%d' % (i)] = arc_hdu[0].header['WLSFIT_%d' % (i)]
        hdu.append(fits.ImageHDU(data=wls_2d, name='WAVELENGTH.RAW'))

        in_data = hdu['SCI.CRJ'].data if 'SCI.CRJ' in hdu else hdu['SCI'].data
        skylines, skyline_list, skylines_ref_y = prep_science.find_nightsky_lines(
            data=numpy.array(in_data),
            linewidth=linewidth,
        )

        #
        # TODO: CONVERT SKYLINE POSITION FROM PIXELS TO WAVELENGTHS
        #

        #
        # Fit and include the wavelength distortion (based on sky-lines) in the wavelength calibration
        #
        if (options.model_wl_distortions):
            print "\n"*10
            print "symmetry:", reference_row
            print "spec ref row:", skylines_ref_y
            print "from model:", hdu[0].header['RSSYCNTR']
            print "binning x/y: ", binx, biny
            print "\n"*10
            distortion_2d, dist_quality = model_distortions.map_wavelength_distortions(
                skyline_list=skyline_list,
                wl_2d=wls_2d,
                img_2d=img_crjclean,
                diff_2d=None,
                badrows=bad_rows_img,
                linewidth=linewidth,
                xbin=binx, ybin=biny,
                ref_row=skylines_ref_y,
                symmetry_row=hdu[0].header['RSSYCNTR'], #reference_row*biny,
                primary_header=hdu[0].header,
                debug=options.debug,
            )
            fits.PrimaryHDU(data=distortion_2d).writeto(
                "specred.wl.dist.fits", clobber=True)
            if (distortion_2d is not None):
                max_dist = 1.5
                # TODO: CHANGE TO BE DEPENDENT ON SPECTRAL RESOLUTION ETC.
                distortion_2d[distortion_2d > max_dist] = max_dist
                distortion_2d[distortion_2d < -1*max_dist] = -1*max_dist
        else:
            logger.info("Per user-request skipping WL distortion modeling")
            distortion_2d = None

        if (distortion_2d is not None):
            wls_2d -= distortion_2d
            hdu.append(fits.ImageHDU(data=distortion_2d, name='WAVELENGTH.DISTORTION'))
            hdu.append(fits.ImageHDU(data=wls_2d, name='WAVELENGTH'))
        else:
            logger.warning("Skipping the wavelength distortion due to "
                           "previous error")

        hdu.writeto("dummy.fits", clobber=True)
        #os._exit(0)

        fits.PrimaryHDU(data=img_data).writeto("img0.fits", clobber=True)

        apply_skyline_intensity_flat = False
        if (apply_skyline_intensity_flat):
            # 
            # Extract the sky-line intensity profile along the slit. Use this to 
            # correct the data. This should also improve the quality of the extracted
            # 2-D sky.
            #
            plot_filename = "%s_slitprofile.png" % (fb)
            skylines, skyline_list, intensity_profile = \
                prep_science.extract_skyline_intensity_profile(
                    hdulist=hdu,
                    data=numpy.array(hdu['SCI.RAW'].data),
                    wls=wls_fit,
                    plot_filename=plot_filename,
                )
            # Flatten the science frame using the line profile
            # hdu.append(
            #     fits.ImageHDU(
            #         data=numpy.array(hdu['SCI'].data),
            #         header=hdu['SCI'].header,
            #         name="SCI.PREFLAT"
            #     )
            # )
            # hdu.append(
            #     fits.ImageHDU(
            #         data=numpy.array(hdu['SCI'].data / intensity_profile.reshape((-1, 1))),
            #         header=hdu['SCI'].header,
            #         name="SCI.POSTFLAT"
            #     )
            # )

            #
            # Mask out all regions with relative intensities below 0.1x max 
            #
            stats = scipy.stats.scoreatpercentile(intensity_profile, [50, 16, 84, 2.5, 97.5])
            one_sigma = (stats[4] - stats[3]) / 4.
            median = stats[0]
            bad_region = intensity_profile < median - 2 * one_sigma
            hdu['SCI'].data[bad_region] = numpy.NaN
            intensity_profile[bad_region] = numpy.NaN

            hdu['SCI'].data /= intensity_profile.reshape((-1, 1))
            logger.info("Slit-flattened SCI extension")

            y = img_data / intensity_profile.reshape((-1, 1))
            fits.PrimaryHDU(data=(y / img_data)).writeto("img1.fits", clobber=True)

            # img_data /= intensity_profile.reshape((-1,1))
        else:
            pass

        if (options.debug):
            # print "FOUND NIGHT-SKY LINES:"
            # numpy.savetxt(sys.stdout, skyline_list, "%9.3f")
            numpy.savetxt("nightsky_lines", skyline_list)

        skyline_tbhdu = prep_science.add_skylines_as_tbhdu(skyline_list)
        skyline_tbhdu.header['LINEREFY'] = skylines_ref_y
        hdu_appends.append(skyline_tbhdu)

        #
        # Map wavelength distortions
        #
        # try:
        #     print skyline_list.shape
        #     distortions, distortions_binned = map_distortions.map_distortions(
        #         wl_2d=wls_2d,
        #         diff_2d=None,
        #         img_2d = img_raw,
        #         y=610,
        #         x_list=skyline_list[:,0],
        #     )
        # except:
        #     pass

        # logger.info("Adding xxx extension")
        # hdu.append(fits.ImageHDU(header=hdu['SCI'].header,
        #                          data=img_data,
        #                          name="XXX"))

        #
        # Compute a full-frame 2-D flat-field.
        # With this flat-field we can extract a better sky spectrum, and later improve the sky-subtraction
        #
        logger.info("Computing 2-D flatfield from night sky intensity profile")
        vph_flatfield, vph_flat_interpol = \
            fiddle_slitflat2.create_2d_flatfield_from_sky(
                wl=wls_2d,
                img=img_data,
                bad_rows=bad_rows
        )
        flattened_img = img_data / vph_flatfield
        logger.info("Flattened image: %s" % (str(flattened_img.shape)))



        # #
        # # Now go ahead and extract the full 2-d sky
        # #
        # logger.info("Extracting 2-D sky")
        # sky_regions = numpy.array([[0, hdu['SCI'].data.shape[0]]])
        # sky2d = skysub2d.make_2d_skyspectrum(
        #     hdu,
        #     wls_2d,
        #     sky_regions=sky_regions,
        #     oversample_factor=1.0,
        #     slitprofile=None, #slitprofile,
        #     )

        # logger.info("Performing sky subtraction")
        # sky_hdu = fits.ImageHDU(data=sky2d, name='SKY')
        # hdu.append(sky_hdu)

        # if (not slitprofile == None):
        #     sky_hdux = fits.ImageHDU(data=sky2d*slitprofile.reshape((-1,1)))
        #     sky_hdux.name = "SKY_X"
        #     hdu.append(sky_hdux)

        # # Don't forget to subtract the sky off the image
        # for source_ext in ['SCI', 'SCI.NOCRJ']:
        #     hdu[source_ext].data -= sky2d #(sky2d * slitprofile.reshape((-1,1)))

        # numpy.savetxt("OBJ_%s_slit.asc" % (fb[:-5]), slitprofile)

        #
        # Compute the optimized sky, using better-chosen spline basepoints 
        # to sample the sky-spectrum
        #
        sky_regions = numpy.array([[300, 500], [1400, 1700]])
        logger.info("Preparing optimized sky-subtraction")
        ia = None

        # simple_spec = optimalskysub.optimal_sky_subtraction(hdu, 
        #                                       sky_regions=sky_regions,
        #                                       N_points=1000,
        #                                       iterate=False,
        #                                       skiplength=10, 
        #                                       return_2d=False)
        # numpy.savetxt("%s.simple_spec" % (_fb), simple_specs)

        # simple_spec = hdu['VAR'].data[hdu['VAR'].data.shape[0]/2,:]
        # numpy.savetxt("%s.simple_spec_2" % (_fb), simple_spec)

        # logger.info("Searching for and analysing sky-lines")
        # skyline_list = wlcal.find_list_of_lines(simple_spec, readnoise=1, avg_width=1)
        # print skyline_list

        # logger.info("Creating spatial flatfield from sky-line intensity profiles")
        # i, ia, im = skyline_intensity.find_skyline_profiles(hdu, skyline_list)


        if (apply_skyline_intensity_flat):
            skyline_flat = intensity_profile.reshape((-1, 1))
        else:
            skyline_flat = None

        # sky_2d, spline = optimalskysub.optimal_sky_subtraction(
        #     hdu, 
        #     sky_regions=sky_regions,
        #     N_points=2000,
        #     iterate=False,
        #     skiplength=5,
        #     skyline_flat=skyline_flat, #intensity_profile.reshape((-1,1)),
        # )



        sky_2d, spline, extra = optimalskysub.optimal_sky_subtraction(
            hdu,
            sky_regions=None,  # sky_regions,
            N_points=600,
            iterate=False,
            skiplength=5,
            skyline_flat=skyline_flat,  # intensity_profile.reshape((-1,1)),
            # select_region=numpy.array([[900,950]])
            # select_region=numpy.array([[600, 640], [660, 700]]),
            wlmode=options.wlmode,
            debug_prefix="%s__" % (fb[:-5]),
            image_data=flattened_img,
            obj_wl=wls_2d,
            debug=options.debug,
            noise_mode=options.sky_noise_mode,
        )
        if (sky_2d is not None):
            (x_eff, wl_map, medians, p_scale, p_skew, fm, good_sky_data) = extra

            hdu.append(fits.ImageHDU(data=good_sky_data.astype(numpy.int),
                                     name="GOOD_SKY_DATA"))
        else:
            logger.critical("Error while computing sky spectrum")
            good_sky_data = None

        #
        # Create a diagnostic plot showing the sky-spectrum and the
        # sky-fit spline used for sky-subtraction
        #
        plot_high_res_sky_spec.plot_sky_spectrum(
            wl=wls_2d,
            flux=flattened_img,
            good_sky_data=good_sky_data,
            bad_rows=bad_rows,
            output_filebase=output_basename+".skyspec",
            sky_spline=spline,
            ext_list=['png'],
        )

        #
        # Save a high-res version of the sky-spectrum as 1-D fits for
        # wavelength verification and other purposes
        #
        hdu.append(save_sky_spec(wl=wls_2d, sky_spline=spline))

        # recompute sky-2d based on the full wavelength map and the spline interpolator
        # sky_2d = spline(wls_2d)

        # bs = 100
        # maxbs = 10

        # sky2d_full = numpy.zeros(img_data.shape)
        # for nbs in range(maxbs):

        #     sky_2d, spline, extra = optimalskysub.optimal_sky_subtraction(
        #         hdu, 
        #         sky_regions=None, #sky_regions,
        #         N_points=2000,
        #         iterate=False,
        #         skiplength=5,
        #         skyline_flat=skyline_flat, #intensity_profile.reshape((-1,1)),
        #         #select_region=numpy.array([[900,950]])
        #         select_region=numpy.array([[nbs*bs,(nbs+1)*bs]])
        #     )
        #     extra = 
        #     if (sky_2d == None):
        #         continue
        #     sky2d_full[nbs*bs:(nbs+1)*bs, :] = sky_2d[nbs*bs:(nbs+1)*bs, :]

        # sky_2d = sky2d_full

        try:
            if (skyline_flat is not None):
                fits.PrimaryHDU(data=hdu['SCI.RAW'].data / skyline_flat).writeto("img_sky2d_input_skylineflat.fits",
                                                                             clobber=True)
            fits.PrimaryHDU(data=hdu['SCI.RAW'].data / fm.reshape((-1, 1))).writeto("img_sky2d_input_fm.fits", clobber=True)

            fits.PrimaryHDU(data=sky_2d).writeto("img_sky2d.fits", clobber=True)

            fits.PrimaryHDU(data=(sky_2d*vph_flatfield)).writeto("img_sky2d_x_vphflat.fits", clobber=True)

            fits.PrimaryHDU(data=(img_data - (sky_2d*vph_flatfield))).writeto("img_vphflat_skysub.fits", clobber=True)
        except:
            pass
        #
        # Add here:
        #
        # Step 1:
        # iteratively check the noise in and around sky-lines. Weight noise with
        # the amplitude of the sky-spectrum. Then compute local (in ~ten bands 
        # across the image) scaling factor that minimizes residuals. Take care 
        # to mask out sources first. Then compute smooth scaling actor that yields
        # the best overall sky subtraction.
        #
        logger.info("Minimizing sky residuals")
        # scaling_data, opt_sky_scaling = optscale.minimize_sky_residuals(
        #     img_data, sky_2d, vert_size=5, smooth=20, debug_out=True)
        # # opt_sky_scaling = fm.reshape((-1,1))
        # numpy.savetxt(out_filename[:-5]+".skyscaling", opt_sky_scaling)

        skyscaling2d = 1.
        opt_sky_scaling = 1.

        fits.PrimaryHDU(data=img_data).writeto("debug_minimizeskyresiduals_img.fits", clobber=True)
        fits.PrimaryHDU(data=sky_2d).writeto("debug_minimizeskyresiduals_sky2d.fits", clobber=True)
        fits.PrimaryHDU(data=wl_map).writeto("debug_minimizeskyresiduals_wlmap.fits", clobber=True)

        if (options.skyscaling == 'none'):

            skyscaling2d = numpy.ones(img_data.shape)

            pass

        elif (options.skyscaling == 's2d'):

            full2d, data, pf2, data2, spline2d = optscale.minimize_sky_residuals2_spline(
                img=img_data,
                sky=sky_2d,
                wl=wl_map,
                bpm=hdu['BPM'].data,
                vert_size=-25,
                dl=-25)
            numpy.savetxt("new_scaling.dump", data)
            skyscaling2d = spline2d

            pass

        elif (options.skyscaling == 'p2d'):
            pass

            ret = optscale.minimize_sky_residuals2(
                img=img_data,
                sky=sky_2d,
                wl=wl_map,
                bpm=hdu['BPM'].data,
                vert_size=-25,
                dl=-25)
            if (ret is not None):
                full2d, data, pf2, data2 = ret
                numpy.savetxt("new_scaling.dump", data)
            else:
                logger.error("Unable to optimize sky subtraction, continuing without optimization")
                full2d = numpy.ones(img_data.shape)
                data = None
                pf2 = None
                data2 = None

            opt_sky_scaling = full2d
            skyscaling2d = full2d

        else:

            skyscaling2d = vph_flatfield

        # data, filtered, full2d = optscale.minimize_sky_residuals2(
        #     img=img_data, 
        #     sky=sky_2d, 
        #     wl=wl_map, 
        #     bpm=hdu['BPM'].data,
        #     vert_size=-25, 
        #     dl=-25)
        # numpy.savetxt("new_scaling.dump", data)

        #
        # step 2: 
        # Also consider small-scale gaussian smoothing to more closely match the
        # sky-line profile along the slit.
        #
        pass

        # skysub = obj_data - sky2d
        # ss_hdu = fits.ImageHDU(header=obj_hdulist['SCI.RAW'].header,
        #                          data=skysub)
        # ss_hdu.name = "SKYSUB.OPT"
        # obj_hdulist.append(ss_hdu)

        ss_hdu2 = fits.ImageHDU(header=hdu['SCI'].header,
                                data=(sky_2d * skyscaling2d),
                                name="SKYSUB.IMG")
        # ss_hdu2 = fits.ImageHDU(header=hdu['SCI'].header,
        #                          data=(sky_2d * opt_sky_scaling))
        #ss_hdu2.name = "SKYSUB.IMG"
        hdu.append(ss_hdu2)

        ss_hdu2 = fits.ImageHDU(header=hdu['SCI'].header,
                                data=(sky_2d),
                                name="SKY.RAW")
        hdu.append(ss_hdu2)

        # hdu.append(fits.ImageHDU(header=hdu['SCI'].header,
        #                          data=wl_map,
        #                          name="WL_XXX")
        #            )
        hdu.append(fits.ImageHDU(header=hdu['SCI'].header,
                                 data=skyscaling2d,
                                 name="SKY.SCALE")
                   )

        skysub_img = (img_data) - (sky_2d * skyscaling2d)  # opt_sky_scaling)
        # skysub_hdu = fits.ImageHDU(header=hdu['SCI'].header,
        #                            data=numpy.array(skysub_img),
        #                            name="SKYSUB.OPT")
        # hdu.append(skysub_hdu)

        #
        # Run cosmic ray rejection on the sky-line subtracted frame
        # Loop over all SCI extensions
        #
        # median_sky = numpy.median(sky_2d * opt_sky_scaling)
        median_sky = bottleneck.nanmedian(sky_2d * opt_sky_scaling)
        sigclip = 5.0
        sigfrac = 0.6
        objlim = 5.0
        saturation_limit = 65000
        try:
            gain = 1.5 if (not 'GAIN' in hdu['SCI'].header) else hdu['SCI'].header['GAIN']
            readnoise = 3 if (not 'RDNOISE' in hdu['SCI'].header) else hdu['SCI'].header['RDNOISE']
        except:
            gain, readnoise = 1.3, 5

        crj = podi_cython.lacosmics(
            numpy.array(skysub_img + median_sky),  # .astype(numpy.float64),
            gain=gain,
            readnoise=readnoise,
            niter=3,
            sigclip=sigclip, sigfrac=sigfrac, objlim=objlim,
            saturation_limit=saturation_limit,
            verbose=False
        )
        cell_cleaned, cell_mask, cell_saturated = crj

        hdu.append(fits.ImageHDU(header=hdu['SCI'].header,
                                 data=skysub_img + median_sky - cell_cleaned,
                                 name="COSMICS"))

        final_hdu = fits.ImageHDU(header=hdu['SCI'].header,
                                  data=(cell_cleaned - median_sky),
                                  name="SKYSUB.OPT")
        hdu.append(final_hdu)


        #
        # Add some more post-processing here:
        # - source detection
        # - optimal extraction of all detected sources
        #

        # compute a source list (includes source position, extent, and intensity)
        extract1d = options.extract1d
        still_good = True
        if (extract1d or options.rectify):
            prof, prof_var = find_sources.continuum_slit_profile(
                data=hdu['SKYSUB.OPT'].data.copy(),
                sky=hdu['SKYSUB.IMG'].data.copy(),
                wl=wls_2d,
                var=hdu['VAR'].data.copy(),
            )
            if  (prof is None or prof_var is None):
                logger.warning("Unable to extract 1-D spectra")
                still_good = False
            else:
                numpy.savetxt("source_profile", prof)
                src_profile_imghdu = find_sources.save_continuum_slit_profile(
                    prof=prof,
                    prof_var=prof_var)
                hdu_appends.append(src_profile_imghdu)

        if ((extract1d or options.rectify) and still_good):
            sources = find_sources.identify_sources(prof, prof_var)

            if (sources is None):
                still_good = False
            else:
                #
                # Create a ds9-compatible region file to allow user-friendly
                #  inspection of all detected source.
                #
                find_sources.write_source_region_file(
                    img_shape=hdu['SCI'].data.shape,
                    sources=sources,
                    outfile="OBJ_%s.sources.reg" % (fb[:-5]),
                )

                #
                # Also prepare to save all source information as TableHDU in
                #  the output file
                #
                source_tbhdu = find_sources.create_source_tbhdu(sources)
                hdu_appends.append(source_tbhdu)

        # if ((not extract1d) or
        #     (extract1d and sources.shape[0]<= 0)):
        #     logger.warning("No sources detected, skipping source extraction")
        # else:
        if (still_good and (extract1d or options.rectify) and
            sources.shape[0]>0):
            fullframe_background = zero_background.find_background_correction(
                img_data=hdu['SKYSUB.OPT'].data.copy(),
                sources=sources,
                badrows=bad_rows,
            )
            if (fullframe_background is not None):
                hdu['SKYSUB.OPT'].data -= fullframe_background
                hdu.append(fits.ImageHDU(data=fullframe_background,
                                         name="SKY.RESIDUALS"))

            # now pick the brightest of all sources
            i_brightest = numpy.argmax(sources[:, 1])
            print i_brightest
            print sources[i_brightest]
            brightest = sources[i_brightest]

            # Now trace the line
            logger.info("computing spectrum trace")
            spec_data = hdu['SKYSUB.OPT'].data.copy()
            center_x = spec_data.shape[1] / 2
            spectrace_data = tracespec.compute_spectrum_trace(
                data=spec_data,
                start_x=center_x,
                start_y=brightest[0],
                xbin=5)

            logger.info("finding trace slopes")
            slopes, trace_offset = tracespec.compute_trace_slopes(
                spectrace_data)
            hdu_appends.append(tracespec.save_trace_offsets(trace_offset))

            # print slopes
            #hdu[0].header['TRACE0_0']
            pass
        else:
            still_good = False

        if (still_good and options.rectify):
            logger.info("Starting to rectify the SCI and VAR planes")
            rect_flux, rect_var = rectify_fullspec.rectify_full_spec(
                data=hdu['SKYSUB.OPT'].data.copy(),
                var=hdu['VAR'].data.copy(),
                wavelength=wls_2d,
                traceoffset=trace_offset,
            )
            logger.debug("done rectifying")
            logger.debug("appending SCI.RECT extension")
            hdu.append(rect_flux)
            logger.debug("appending VAR.RECT extension")
            hdu.append(rect_var)


        if (still_good and extract1d):

            for source_id, source in enumerate(sources):

                logger.info("generating source profile in prep for optimal "
                            "extraction - source %d" % (source_id + 1))
                width = 2 * (source[3] - source[2])
                source_profile_2d = optimal_extraction.generate_source_profile(
                    data=hdu['SKYSUB.OPT'].data,
                    variance=hdu['VAR'].data,
                    wavelength=wls_2d,
                    trace_offset=trace_offset,
                    position=[center_x, source[0]],
                    width=width,
                )
                #print "source profile 2d", source_profile_2d.shape, \
                #    "\n", source_profile_2d

                logger.info("computing optimal extraction weights")
                supersample = 2
                optimal_weight = optimal_extraction.integrate_source_profile(
                    width=width,
                    supersample=supersample,
                    profile2d=source_profile_2d,
                    wl_resolution=-5,
                )
                logger.info("done with weights, ready for extraction!")

                #
                # Extract the 1-d spectrum, applying weights, and
                # re-drizzling all flux to a simple wavelength grid using
                # twice the mean dispersion (in A/px) of the input data
                #
                min_wl, max_wl = numpy.min(wls_2d), numpy.max(wls_2d)
                mean_dispersion = (max_wl - min_wl) / wls_2d.shape[1]
                output_dispersion = numpy.round(0.5*mean_dispersion, 2)
                d_width = source[2:4] - source[0]
                y_ranges = [d_width]
                results = optimal_extraction.optimal_extract(
                    img_data=hdu['SKYSUB.OPT'].data,
                    wl_data=wls_2d,
                    variance_data=hdu['VAR'].data,
                    trace_offset=trace_offset,
                    optimal_weight=optimal_weight,
                    opt_weight_center_y=source[0],
                    reference_x=center_x,
                    reference_y=source[0],
                    y_ranges=y_ranges,
                    dwl=0.5*mean_dispersion,
                    debug_filebase=fb[:-5]+"__" if options.debug else None,
                )

                #
                # extract individual data from return data
                #
                spectra_1d = results['spectra']
                variance_1d = results['variance']
                wl0 = results['wl0']
                dwl = results['dwl']
                out_wl = results['wl_base']

                #
                # Finally, merge wavelength data and flux and write output to file
                #
                output_format = ["ascii", "fits"]
                # out_fn = "opt_extract"
                if ("fits" in output_format or True):
                    # out_fn_fits = out_fn + ".fits"
                    # logger.info("Writing FITS output to %s" % (out_fn))
                    #
                    # extlist = [fits.PrimaryHDU()]

                    spec1d_hdus = []
                    for i, part in enumerate(['BEST', 'WEIGHTED', 'SUM']):
                        spec1d_hdus.append(
                            fits.ImageHDU(data=spectra_1d[:, :, i].T,
                                          name="SCI.%s.%d" % (part, source_id+1), )
                        )
                        spec1d_hdus.append(
                            fits.ImageHDU(data=variance_1d[:, :, i].T,
                                          name="VAR.%s.%d" % (part, source_id+1), )
                        )

                    # add headers for the wavelength solution
                    for ext in spec1d_hdus:  # ['SCI', 'VAR']:
                        ext.header['WCSNAME'] = "calibrated wavelength"
                        ext.header['CRPIX1'] = 1.
                        ext.header['CRVAL1'] = wl0
                        ext.header['CD1_1'] = dwl
                        ext.header['CTYPE1'] = "AWAV"
                        ext.header['CUNIT1'] = "Angstrom"
                        for i, yr in enumerate(y_ranges):
                            keyname = "YR_%03d" % (i + 1)
                            value = "%04d:%04d" % (yr[0], yr[1])
                            ext.header[keyname] = (
                            value, "y-range for aperture %d" % (i + 1))
                    #hdulist.writeto(out_fn_fits, clobber=True)

                    hdu_appends.extend(spec1d_hdus)
                    #logger.info("done writing results (%s)" % (out_fn_fits))

                if ("ascii" in output_format):
                    out_fn_ascii = "OBJ_%s.%d.dat" % (fb[:-5], source_id+1)
                    out_fn_asciivar = "OBJ_%s.%d.var" % (fb[:-5], source_id+1)
                    logger.info("Writing output as ASCII to %s / %s" % (out_fn_ascii,
                                                                        out_fn_asciivar))

                    with open(out_fn_ascii, "w") as of:
                        for aper, yr in enumerate(y_ranges):
                            print >> of, "# APERTURE: ", yr
                            numpy.savetxt(of, numpy.append(out_wl.reshape((-1, 1)),
                                                           spectra_1d[:, aper, :],
                                                           axis=1
                                                           )
                                          )
                            print >> of, "\n" * 5

                    with open(out_fn_asciivar, "w") as of:
                        for aper, yr in enumerate(y_ranges):
                            print >> of, "# APERTURE: ", yr
                            numpy.savetxt(of, numpy.append(out_wl.reshape((-1, 1)),
                                                           variance_1d[:, aper, :],
                                                           axis=1
                                                           )
                                          )
                            print >> of, "\n" * 5
                    # numpy.savetxt(out_fn + ".var",
                    #               numpy.append(out_wl.reshape((-1, 1)),
                    #                            variance_1d, axis=1))
                    logger.info("done writing ASCII results")

        hdu.extend(hdu_appends)

        #
        # And finally write reduced frame back to disk
        #
        out_filename = "OBJ_%s" % (fb)
        logger.info("Saving output to %s" % (out_filename))
        pysalt.clobberfile(out_filename)
        hdu.writeto(out_filename, clobber=True)









        # #
        # # Trial: replace all 0 value pixels with NaNs
        # #
        # bpm = hdu[3].data
        # hdu[1].data[bpm == 1] = numpy.NaN

        # # for ext in hdu[1:]:
        # #     ext.data[ext.data <= 0] = numpy.NaN


        # spectrectify writes to disk, no need to do so here
        # specrectify(mosaic_filename, outimages=out_filename, outpref='', 
        #             solfile=dbfile, caltype='line', 
        #             function='legendre',  order=3, inttype='interp', 
        #             w1=None, w2=None, dw=None, nw=None,
        #             blank=0.0, clobber=True, logfile=logfile, verbose=True)

        # #
        # # Now we have a full 2-d spectrum, but still with emission lines
        # #

        # #
        # # Next, find good regions with no source contamation
        # #
        # hdu_rect = pyfits.open(out_filename)
        # hdu_rect.info()

        # src_region = [1500,2400] # Jay
        # src_region = [1850,2050] # Greg

        # #intspec = get_integrated_spectrum(hdu_rect, out_filename)
        # #slitprof, skymask = find_slit_profile(hdu_rect, out_filename) # Jay
        # slitprof, skymask = find_slit_profile(hdu_rect, out_filename, src_region)  # Greg
        # print skymask.shape[0]

        # hdu_rect['SCI'].data /= slitprof

        # rectflat_filename = "OBJ_flat_%s" % (fb)
        # pysalt.clobberfile(rectflat_filename)
        # hdu_rect.writeto(rectflat_filename, clobber=True)

        # #
        # # Block out the central region of the chip as object
        # #
        # skymask[src_region[0]/biny:src_region[1]/biny] = False
        # sky_lines = bottleneck.nanmedian(
        #     hdu_rect['SCI'].data[skymask].astype(numpy.float64),
        #     axis=0)
        # print sky_lines.shape

        # #
        # # Now subtract skylines
        # #
        # hdu_rect['SCI'].data -= sky_lines
        # skysub_filename = "OBJ_skysub_%s" % (fb)
        # pysalt.clobberfile(skysub_filename)
        # hdu_rect.writeto(skysub_filename, clobber=True)

    return

    verbose = False
    for idx, filename in enumerate(obslog['FLAT']):
        cur_op = 0

        dirname, filebase = os.path.split(filename)
        logger.info("basic reduction for frame %s (%s)" % (filename, filebase))

        hdu = salt_prepdata(filename, badpixelimage=None, create_variance=False,
                            verbose=False)

        out_filename = "prep_" + filebase
        pysalt.clobberfile(out_filename)
        hdu.writeto(out_filename, clobber=True)

        # #
        # # 
        # #

        # #
        # # Prepare basic header stuff
        # #
        # after_prepare = "%s/%s/%s" % (work_dir, reduction_steps[cur_op], filebase)
        # print after_prepare
        # saltprepare(filename, after_prepare, '', createvar=False, 
        #             badpixelimage='', clobber=True, logfile=logfile, verbose=verbose)
        # cur_op += 1

        # #
        # # Apply bias subtraction
        # #
        # after_bias = "%s/%s/%s" % (work_dir, reduction_steps[cur_op], filebase)
        # saltbias(after_prepare, after_bias, '', subover=True, trim=True, subbias=False, masterbias='',  
        #       median=False, function='polynomial', order=5, rej_lo=3.0, rej_hi=5.0, 
        #       niter=10, plotover=False, turbo=False, 
        #          clobber=True, logfile=logfile, verbose=verbose)
        # cur_op += 1

        # #
        # # gain correct the data
        # #
        # #
        # after_gain = "%s/%s/%s" % (work_dir, reduction_steps[cur_op], filebase)
        # saltgain(after_bias, after_gain, '', 
        #          usedb=False, 
        #          mult=True, 
        #          clobber=True, 
        #          logfile=logfile, 
        #          verbose=verbose)
        # cur_op += 1

        # #
        # # cross talk correct the data
        # #
        # after_xtalk = "%s/%s/%s" % (work_dir, reduction_steps[cur_op], filebase)
        # saltxtalk(after_gain, after_xtalk, '', 
        #           xtalkfile = "", 
        #           usedb=False, 
        #           clobber=True, 
        #           logfile=logfile, 
        #           verbose=verbose)
        # cur_op += 1

        # #
        # # cosmic ray clean the data
        # # only clean the object data
        # #
        # after_crj = "%s/%s/%s" % (work_dir, reduction_steps[cur_op], filebase)
        # if obs_dict['CCDTYPE'][idx].count('OBJECT') and obs_dict['INSTRUME'][idx].count('RSS'):
        #     #img='xgbp'+os.path.basename(infile_list[i])
        #     saltcrclean(after_xtalk, after_crj, '', 
        #                 crtype='edge', thresh=5, mbox=11, bthresh=5.0,
        #                 flux_ratio=0.2, bbox=25, gain=1.0, rdnoise=5.0, fthresh=5.0, bfactor=2,
        #                 gbox=3, maxiter=5, 
        #                 multithread=True,  
        #                 clobber=True, 
        #                 logfile=logfile, 
        #                 verbose=verbose)
        # else:
        #     after_crj = after_xtalk
        # cur_op += 1

        # #
        # # flat field correct the data
        # #

        # flat_imgs=''
        # for i in range(len(infile_list)):
        #   if obs_dict['CCDTYPE'][i].count('FLAT'):
        #      if flat_imgs: flat_imgs += ','
        #      flat_imgs += 'xgbp'+os.path.basename(infile_list[i])

        # if len(flat_imgs)!=0:
        #    saltcombine(flat_imgs,flatimage, method='median', reject=None, mask=False,    \
        #           weight=True, blank=0, scale='average', statsec='[200:300, 600:800]', lthresh=3,    \
        #           hthresh=3, clobber=True, logfile=logfile, verbose=True)
        #    saltillum(flatimage, flatimage, '', mbox=11, clobber=True, logfile=logfile, verbose=True)

        #    saltflat('xgbpP*fits', '', 'f', flatimage, minflat=500, clobber=True, logfile=logfile, verbose=True)
        # else:
        #    flats=None
        #    imfiles=glob.glob('cxgbpP*fits')
        #    for f in imfiles:
        #        shutil.copy(f, 'f'+f)

        # #mosaic the data
        # #geomfile=iraf.osfn("pysalt$data/rss/RSSgeom.dat")
        # geomfile=pysalt.get_data_filename("pysalt$data/rss/RSSgeom.dat")
        # saltmosaic('fxgbpP*fits', '', 'm', geomfile, interp='linear', cleanup=True, geotran=True, clobber=True, logfile=logfile, verbose=True)

    return

    sys.exit(0)

    if imreduce:
        # prepare the data
        saltprepare(infiles, '', 'p', createvar=False, badpixelimage='', clobber=True, logfile=logfile, verbose=True)

        # bias subtract the data
        saltbias('pP*fits', '', 'b', subover=True, trim=True, subbias=False, masterbias='',
                 median=False, function='polynomial', order=5, rej_lo=3.0, rej_hi=5.0,
                 niter=10, plotover=False, turbo=False,
                 clobber=True, logfile=logfile, verbose=True)

        # gain correct the data
        saltgain('bpP*fits', '', 'g', usedb=False, mult=True, clobber=True, logfile=logfile, verbose=True)

        # cross talk correct the data
        saltxtalk('gbpP*fits', '', 'x', xtalkfile="", usedb=False, clobber=True, logfile=logfile, verbose=True)

        # cosmic ray clean the data
        # only clean the object data
        for i in range(len(infile_list)):
            if obs_dict['CCDTYPE'][i].count('OBJECT') and obs_dict['INSTRUME'][i].count('RSS'):
                img = 'xgbp' + os.path.basename(infile_list[i])
                saltcrclean(img, img, '', crtype='edge', thresh=5, mbox=11, bthresh=5.0,
                            flux_ratio=0.2, bbox=25, gain=1.0, rdnoise=5.0, fthresh=5.0, bfactor=2,
                            gbox=3, maxiter=5, multithread=True, clobber=True, logfile=logfile, verbose=True)

        # flat field correct the data
        flat_imgs = ''
        for i in range(len(infile_list)):
            if obs_dict['CCDTYPE'][i].count('FLAT'):
                if flat_imgs: flat_imgs += ','
                flat_imgs += 'xgbp' + os.path.basename(infile_list[i])

        if len(flat_imgs) != 0:
            saltcombine(flat_imgs, flatimage, method='median', reject=None, mask=False, \
                        weight=True, blank=0, scale='average', statsec='[200:300, 600:800]', lthresh=3, \
                        hthresh=3, clobber=True, logfile=logfile, verbose=True)
            saltillum(flatimage, flatimage, '', mbox=11, clobber=True, logfile=logfile, verbose=True)

            saltflat('xgbpP*fits', '', 'f', flatimage, minflat=500, clobber=True, logfile=logfile, verbose=True)
        else:
            flats = None
            imfiles = glob.glob('cxgbpP*fits')
            for f in imfiles:
                shutil.copy(f, 'f' + f)

        # mosaic the data
        # geomfile=iraf.osfn("pysalt$data/rss/RSSgeom.dat")
        geomfile = pysalt.get_data_filename("pysalt$data/rss/RSSgeom.dat")
        saltmosaic('fxgbpP*fits', '', 'm', geomfile, interp='linear', cleanup=True, geotran=True, clobber=True,
                   logfile=logfile, verbose=True)

        # clean up the images
        if cleanup:
            for f in glob.glob('p*fits'): os.remove(f)
            for f in glob.glob('bp*fits'): os.remove(f)
            for f in glob.glob('gbp*fits'): os.remove(f)
            for f in glob.glob('xgbp*fits'): os.remove(f)
            for f in glob.glob('fxgbp*fits'): os.remove(f)

    # set up the name of the images
    if specreduce:
        for i in range(len(infile_list)):
            if obs_dict['OBJECT'][i].upper().strip() == 'ARC':
                lamp = obs_dict['LAMPID'][i].strip().replace(' ', '')
                arcimage = 'mfxgbp' + os.path.basename(infile_list[i])
                lampfile = pysalt.get_data_filename("pysalt$data/linelists/%s.txt" % lamp)

                specidentify(arcimage, lampfile, dbfile, guesstype='rss',
                             guessfile='', automethod=automethod, function='legendre', order=5,
                             rstep=100, rstart='middlerow', mdiff=10, thresh=3, niter=5,
                             inter=True, clobber=True, logfile=logfile, verbose=True)

                specrectify(arcimage, outimages='', outpref='x', solfile=dbfile, caltype='line',
                            function='legendre', order=3, inttype='interp', w1=None, w2=None, dw=None, nw=None,
                            blank=0.0, clobber=True, logfile=logfile, verbose=True)

    objimages = ''
    for i in range(len(infile_list)):
        if obs_dict['CCDTYPE'][i].count('OBJECT') and obs_dict['INSTRUME'][i].count('RSS'):
            if objimages: objimages += ','
            objimages += 'mfxgbp' + os.path.basename(infile_list[i])

    if specreduce:
        # run specidentify on the arc files

        specrectify(objimages, outimages='', outpref='x', solfile=dbfile, caltype='line',
                    function='legendre', order=3, inttype='interp', w1=None, w2=None, dw=None, nw=None,
                    blank=0.0, clobber=True, logfile=logfile, verbose=True)

    # create the spectra text files for all of our objects
    spec_list = []
    for img in objimages.split(','):
        spec_list.extend(createspectra('x' + img, obsdate, smooth=False, skysection=skysection, clobber=True))
    print spec_list

    # determine the spectrophotometric standard
    extfile = pysalt.get_data_filename('pysalt$data/site/suth_extinct.dat')

    for spec, am, et, pc in spec_list:
        if pc == 'CAL_SPST':
            stdstar = spec.split('.')[0]
            print stdstar, am, et
            stdfile = pysalt.get_data_filename(
                'pysalt$data/standards/spectroscopic/m%s.dat' % stdstar.lower().replace('-', '_'))
            print stdfile
            ofile = spec.replace('txt', 'sens')
            calfile = ofile  # assumes only one observations of a SP standard
            specsens(spec, ofile, stdfile, extfile, airmass=am, exptime=et,
                     stdzp=3.68e-20, function='polynomial', order=3, thresh=3, niter=5,
                     clobber=True, logfile='salt.log', verbose=True)

    for spec, am, et, pc in spec_list:
        if pc != 'CAL_SPST':
            ofile = spec.replace('txt', 'spec')
            speccal(spec, ofile, calfile, extfile, airmass=am, exptime=et,
                    clobber=True, logfile='salt.log', verbose=True)
            # clean up the spectra for bad pixels
            cleanspectra(ofile)


def speccombine(spec_list, obsdate):
    """Combine N spectra"""

    w1, f1, e1 = numpy.loadtxt(spec_list[0], usecols=(0, 1, 2), unpack=True)

    w = w1
    f = 1.0 * f1
    e = e1 ** 2

    for sfile in spec_list[1:]:
        w2, f2, e2 = numpy.loadtxt(sfile, usecols=(0, 1, 2), unpack=True)
        if2 = numpy.interp(w1, w2, f2)
        ie2 = numpy.interp(w1, w2, e2)
        f2 = f2 * numpy.median(f1 / if2)
        f += if2
        e += ie2 ** 2

    f = f / len(spec_list)
    e = e ** 0.5 / len(spec_list)

    sfile = '%s.spec' % obsdate
    fout = open(sfile, 'w')
    for i in range(len(w)):
        fout.write('%f %e %e\n' % (w[i], f[i], e[i]))
    fout.close()


def cleanspectra(sfile, grow=6):
    """Remove possible bad pixels"""
    try:
        w, f, e = numpy.loadtxt(sfile, usecols=(0, 1, 2), unpack=True)
    except:
        w, f = numpy.loadtxt(sfile, usecols=(0, 1), unpack=True)
        e = f * 0.0 + f.std()

    m = (f * 0.0) + 1
    for i in range(len(m)):
        if f[i] <= 0.0:
            x1 = int(i - grow)
            x2 = int(i + grow)
            m[x1:x2] = 0
    m[0] = 0
    m[-1] = 0

    fout = open(sfile, 'w')
    for i in range(len(w)):
        if m[i]:
            fout.write('%f %e %e\n' % (w[i], f[i], e[i]))
    fout.close()


def normalizespectra(sfile, compfile):
    """Normalize spectra by the comparison object"""

    # read in the spectra
    w, f, e = numpy.loadtxt(sfile, usecols=(0, 1, 2), unpack=True)

    # read in the comparison spectra
    cfile = sfile.replace('MCG-6-30-15', 'COMP')
    print cfile
    wc, fc, ec = numpy.loadtxt(cfile, usecols=(0, 1, 2), unpack=True)

    # read in the base star
    ws, fs, es = numpy.loadtxt(compfile, usecols=(0, 1, 2), unpack=True)

    # calcualte the normalization
    ifc = numpy.interp(ws, wc, fc)
    norm = numpy.median(fs / ifc)
    print norm
    f = norm * f
    e = norm * e

    # write out the result
    fout = open(sfile, 'w')
    for i in range(len(w)):
        fout.write('%f %e %e\n' % (w[i], f[i], e[i]))
    fout.close()

    # copy


def createspectra(img, obsdate, minsize=5, thresh=3, skysection=[800, 1000], smooth=False, maskzeros=True,
                  clobber=True):
    """Create a list of spectra for each of the objects in the images"""
    # okay we need to identify the objects for extraction and identify the regions for sky extraction
    # first find the objects in the image
    hdu = fits.open(img)
    target = hdu[0].header['OBJECT']
    propcode = hdu[0].header['PROPID']
    airmass = hdu[0].header['AIRMASS']
    exptime = hdu[0].header['EXPTIME']

    if smooth:
        data = smooth_data(hdu[1].data)
    else:
        data = hdu[1].data

    # replace the zeros with the average from the frame
    if maskzeros:
        mean, std = iterstat(data[data > 0])
        rdata = numpy.random.normal(mean, std, size=data.shape)
        print mean, std
        data[data <= 0] = rdata[data <= 0]

    # find the sections in the images
    section = findobj.findObjects(data, method='median', specaxis=1, minsize=minsize, thresh=thresh, niter=5)
    print section

    # use a region near the center to create they sky
    skysection = findskysection(section, skysection)
    print skysection

    # sky subtract the frames
    shdu = skysubtract(hdu, method='normal', section=skysection)
    if os.path.isfile('s' + img): os.remove('s' + img)
    shdu.writeto('s' + img)

    spec_list = []
    # extract the spectra
    # extract the comparison spectrum
    section = findobj.findObjects(shdu[1].data, method='median', specaxis=1, minsize=minsize, thresh=thresh, niter=5)
    print section
    for j in range(len(section)):
        ap_list = extract(shdu, method='normal', section=[section[j]], minsize=minsize, thresh=thresh, convert=True)
        ofile = '%s.%s_%i_%i.txt' % (target, obsdate, extract_number(img), j)
        write_extract(ofile, [ap_list[0]], outformat='ascii', clobber=clobber)
        spec_list.append([ofile, airmass, exptime, propcode])

    return spec_list


def smooth_data(data, mbox=25):
    mdata = median_filter(data, size=(mbox, mbox))
    return data - mdata


def find_section(section, y):
    """Find the section closest to y"""
    best_i = -1
    dist = 1e5
    for i in range(len(section)):
        d = min(abs(section[i][0] - y), abs(section[i][1] - y))
        if d < dist:
            best_i = i
            dist = d
    return best_i


def extract_number(img):
    """Get the image number only"""
    img = img.split('.fits')
    nimg = int(img[0][-4:])
    return nimg


def iterstat(data, thresh=3, niter=5):
    mean = data.mean()
    std = data.std()
    for i in range(niter):
        mask = (abs(data - mean) < std * thresh)
        mean = data[mask].mean()
        std = data[mask].std()
    return mean, std


def findskysection(section, skysection=[800, 900], skylimit=100):
    """Look through the section lists and determine a section to measure the sky in

       It should be as close as possible to the center and about 200 pixels wide
    """
    # check to make sure it doesn't overlap any existing spectra
    # and adjust if necessary
    for y1, y2 in section:
        if -30 < (skysection[1] - y1) < 0:
            skysection[1] = y1 - 30
        if 0 < (skysection[0] - y2) < 30:
            skysection[0] = y2 + 30
    if skysection[1] - skysection[0] < skylimit: print "WARNING SMALL SKY SECTION"
    return skysection


if __name__ == '__main__':
    logger = pysalt.mp_logging.setup_logging()

    parser = OptionParser()
    parser.add_option("-w", "--wl", dest="wlmode",
                      help="How to create wavelength map (arc/sky/model)",
                      default="model")
    parser.add_option("-s", "--scale", dest="skyscaling",
                      help="How to scale the sky spectrum (none,s2d,p2d)",
                      default="xxx")
    parser.add_option("-d", "--debug", dest="debug",
                       action="store_true", default=False)
    parser.add_option("", "--reusearcs", dest="reusearcs",
                      action="store_true", default=False)
    parser.add_option("", "--nowldist", dest="model_wl_distortions",
                      action="store_false", default=True)
    parser.add_option("", "--noisemode", dest='sky_noise_mode',
                      default="local1")
    parser.add_option("", "--noextract", dest='extract1d',
                      action='store_false', default=True)
    parser.add_option("", "--noflats", dest='use_flats',
                      action='store_false', default=True)
    parser.add_option("", "--arconly", dest='arc_only',
                      action='store_true', default=False)
    parser.add_option("", "--useclosestarc", dest="use_closest_arc",
                      action="store_true", default=False)
    parser.add_option("", "--rectify", dest="rectify",
                      action="store_true", default=False)
    parser.add_option("", "--check", dest="check_only",
                      action="store_true", default=False)

    (options, cmdline_args) = parser.parse_args()

    # print options
    # print cmdline_args

    for raw_dir in cmdline_args[0:]:
        #rawdir = cmdline_args[0]
        prodir = os.path.curdir + '/'
        specred(raw_dir, prodir, options)
    pysalt.mp_logging.shutdown_logging(logger)
