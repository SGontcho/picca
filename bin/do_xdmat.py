import scipy as sp
import fitsio
import argparse
import glob
import healpy
import sys
from scipy import random 
from scipy.interpolate import interp1d

from pylya import constants
from pylya import xcf
from pylya.data import delta
from pylya.data import qso
from pylya import io

from multiprocessing import Pool,Process,Lock,Manager,cpu_count,Value


def calc_dmat(p):
    xcf.fill_neighs(p)
    tmp = xcf.dmat(p)
    return tmp

if __name__ == '__main__':

    parser = argparse.ArgumentParser(formatter_class=argparse.ArgumentDefaultsHelpFormatter)

    parser.add_argument('--out', type = str, default = None, required=True,
                        help = 'output file name')

    parser.add_argument('--drq', type = str, default = None, required=True,
                        help = 'drq')

    parser.add_argument('--in-dir', type = str, default = None, required=True,
                        help = 'data directory')

    parser.add_argument('--rp-max', type = float, default = 200, required=False,
                        help = 'max rp')

    parser.add_argument('--rp-min', type = float, default = -200, required=False,
                        help = 'min rp')

    parser.add_argument('--rt-max', type = float, default = 200, required=False,
                        help = 'max rt')

    parser.add_argument('--np', type = int, default = 100, required=False,
                        help = 'number of r-parallel bins')

    parser.add_argument('--nt', type = int, default = 50, required=False,
                        help = 'number of r-transverse bins')

    parser.add_argument('--lambda-abs', type = float, default = constants.lya, required=False,
                        help = 'wavelength of absorption')

    parser.add_argument('--fid-Om', type = float, default = 0.315, required=False,
                    help = 'Om of fiducial cosmology')

    parser.add_argument('--nside', type = int, default = 8, required=False,
                    help = 'healpix nside')

    parser.add_argument('--nproc', type = int, default = None, required=False,
                    help = 'number of processors')

    parser.add_argument('--z-ref', type = float, default = 2.25, required=False,
                    help = 'reference redshift')

    parser.add_argument('--rej', type = float, default = 1., required=False,
                    help = 'reference redshift')

    parser.add_argument('--z-evol-del', type = float, default = 2.9, required=False,
                    help = 'exponent of the redshift evolution of the delta field')

    parser.add_argument('--z-evol-obj', type = float, default = 1., required=False,
                    help = 'exponent of the redshift evolution of the object field')

    parser.add_argument('--z-min-obj', type = float, default = 1.8, required=False,
                    help = 'min redshift for object field')

    parser.add_argument('--z-max-obj', type = float, default = 5., required=False,
                    help = 'max redshift for object field')

    parser.add_argument('--nspec', type=int,default=None, required=False,
                    help = 'maximum spectra to read')

    parser.add_argument('--no-project', action="store_true", required=False,
                    help = 'do not project out continuum fitting modes')

    args = parser.parse_args()

    if args.nproc is None:
        args.nproc = cpu_count()/2

    print "nproc",args.nproc

    xcf.rp_max = args.rp_max
    xcf.rp_min = args.rp_min
    xcf.rt_max = args.rt_max
    xcf.np = args.np
    xcf.nt = args.nt
    xcf.nside = args.nside
    xcf.zref = args.z_ref
    xcf.alpha = args.z_evol_del
    xcf.lambda_abs = args.lambda_abs
    xcf.rej = args.rej

    cosmo = constants.cosmo(args.fid_Om)

    xcf.angmax = sp.arcsin(xcf.rt_max/(cosmo.r_comoving(constants.boss_lambda_min/args.lambda_abs-1)+xcf.rp_min))

    fi = glob.glob(args.in_dir+"/*.fits.gz")
    dels = {}
    ndels = 0
    for i,f in enumerate(fi):
        sys.stderr.write("\rread {} of {} {}".format(i,len(fi),ndels))
        hdus = fitsio.FITS(f)
        ds = [delta.from_fitsio(h) for h in hdus[1:]]
        ndels+=len(ds)
        phi = [d.ra for d in ds]
        th = [sp.pi/2-d.dec for d in ds]
        pix = healpy.ang2pix(xcf.nside,th,phi)
        for d,p in zip(ds,pix):
            if not p in dels:
                dels[p]=[]
            dels[p].append(d)

            z = 10**d.ll/args.lambda_abs-1
            d.r_comov = cosmo.r_comoving(z)
            d.we *= ((1+z)/(1+args.z_ref))**(xcf.alpha-1)
            if not args.no_project:
                d.project()
        if not args.nspec is None:
            if ndels>args.nspec:break
    sys.stderr.write("\n")

    xcf.dels = dels
    xcf.ndels = ndels
    print "done"

    objs = {}
    ra,dec,zqso,thid,plate,mjd,fid = io.read_drq(args.drq,args.z_min_obj,args.z_max_obj,keep_bal=True)
    phi = ra
    th = sp.pi/2-dec
    pix = healpy.ang2pix(xcf.nside,th,phi)
    print("reading qsos")

    upix = sp.unique(pix)
    for i,ipix in enumerate(upix):
        sys.stderr.write("\r{} of {}".format(i,len(upix)))
        w=pix==ipix
        objs[ipix] = [qso(t,r,d,z,p,m,f) for t,r,d,z,p,m,f in zip(thid[w],ra[w],dec[w],zqso[w],plate[w],mjd[w],fid[w])]
        for q in objs[ipix]:
            q.we = ((1+q.zqso)/(1+args.z_ref))**(args.z_evol_obj-1)
            q.r_comov = cosmo.r_comoving(q.zqso)

    sys.stderr.write("\n")
    xcf.objs = objs

    xcf.counter = Value('i',0)

    xcf.lock = Lock()
    
    cpu_data = {}
    for i,p in enumerate(dels.keys()):
        ip = i%args.nproc
        if not ip in cpu_data:
            cpu_data[ip] = []
        cpu_data[ip].append(p)

    random.seed(0)
    pool = Pool(processes=args.nproc)
    dm = pool.map(calc_dmat,cpu_data.values())
    pool.close()
    dm = sp.array(dm)
    wdm =dm[:,0].sum(axis=0)
    npairs=dm[:,2].sum(axis=0)
    npairs_used=dm[:,3].sum(axis=0)
    dm=dm[:,1].sum(axis=0)

    dm/=wdm


    out = fitsio.FITS(args.out,'rw',clobber=True)
    head = {}
    head['REJ']=args.rej
    head['RPMAX']=xcf.rp_max
    head['RPMIN']=xcf.rp_min
    head['RTMAX']=xcf.rt_max
    head['NT']=xcf.nt
    head['NP']=xcf.np
    head['NPROR']=npairs
    head['NPUSED']=npairs_used

    out.write([wdm,dm],names=['WDM','DM'],header=head)
    out.close()

    