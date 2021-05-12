"""
Set individual bitflags needed for wofls.
"""
import numpy as np
import scipy.ndimage
import xarray
from datacube.utils import masking

from wofs import terrain, constants, boilerplate
from wofs.constants import MASKED_CLOUD, MASKED_CLOUD_SHADOW, NO_DATA


def dilate(array):
    """Dilation e.g. for cloud and cloud/terrain shadow"""
    # kernel = [[1] * 7] * 7 # blocky 3-pixel dilation
    y, x = np.ogrid[-3:4, -3:4]
    kernel = ((x * x) + (y * y) <= 3.5 ** 2)  # disk-like 3-pixel radial dilation
    return scipy.ndimage.binary_dilation(array, structure=kernel)


PQA_SATURATION_BITS = sum(2 ** n for n in [0, 1, 2, 3, 4, 7])  # exclude thermal
PQA_CONTIGUITY_BITS = 0x01FF
PQA_CLOUD_BITS = 0x0C00
PQA_CLOUD_SHADOW_BITS = 0x3000
PQA_SEA_WATER_BIT = 0x0200


@boilerplate.simple_numpify
def pq_filter(pq):
    """
         Propagate flags from the pixel quality product.

         PQ specs: 16 bits.
           0-7 non-saturation of bands 1-5, 6.1, 6.2, 7. (Note bands 6 are thermal, irrelevent to standard WOfS.)
           8 contiguity (presumably including thermal bands)
           9 land (versus sea)
           10-11 no cloud (ACCA, Fmask)
           12-13 no cloud shadow (ACCA, Fmask)
           14 topographic shadow (not implemented)
           15 unspecified

          Over/under-saturation is flagged in the WOfS journal paper, but may not be previously implemented.

          Notes:
            - will output same flag to indicate noncontiguity, oversaturation and undersaturation.
            - disregarding PQ contiguity flag (see eo_filter instead) to exclude thermal bands.
            - permitting simultaneous flags (through addition syntax) since constants happen to be
              different powers of the same base.
            - dilates the cloud and cloud shadow. (Previous implementation eroded the negation.)
            - input must be numpy not xarray.DataArray (due to depreciated boolean fancy indexing behaviour)
    """
    ipq = ~pq  # bitwise-not, e.g. flag cloudiness rather than cloudfree

    masking = np.zeros(ipq.shape, dtype=np.uint8)
    masking[(ipq & (PQA_SATURATION_BITS | PQA_CONTIGUITY_BITS)).astype(np.bool)] = constants.MASKED_NO_CONTIGUITY
    # masking[(ipq & PQA_SEA_WATER_BIT).astype(np.bool)] += constants.MASKED_SEA_WATER
    masking[dilate(ipq & PQA_CLOUD_BITS)] += constants.MASKED_CLOUD
    masking[dilate(ipq & PQA_CLOUD_SHADOW_BITS)] += constants.MASKED_CLOUD_SHADOW
    return masking


C2_NODATA_BITS = 0x0001 # 0001 0th bit
#C2_CLOUD_BITS = #0x000C # 1100 2nd and 3rd bit #try doing them as 2 checks instead of one
C2_CLOUD_BITS = 0x0008 # 1000 3rd bit
C2_CLOUD_SHADOW_BITS = 0x0010 # 0001 0000 4th bit
C2_CLEAR_BITS = 0x0040
C2_CIRRUS_BITS = 0x0004 # 0100 2nd bit
C2_DILATED_BITS = 0x0002 # 0010 1st bit


# could also use clear which is reverse of cloud+dilation or try dilated clouds
# use clear and then also use cirrus as a second check? | is or
# not clear | cirrus
# 

def c2_filter(pq):
    """
         Propagate flags from the pixel quality product.

         PQ specs: 16 bits.
           0 no data # dec 1
           1 dilated cloud # dec 2
           2 cirrus # dec 4
           3 cloud # dec 8
           4 cloud shadow # dec 16
           5 snow # dec 32
           6 clear # dec 64
           7 water # dec 128
           8-9 cloud confidence # dec 256/512
           10-11 cloud shadow confidence # dec 1024/2048
           12-13 snow_ice confidence # dec 4096/8192
           14-15 cirrus confidence # dec 16384/32768

          Over/under-saturation is flagged in the WOfS journal paper, but may not be previously implemented.

          Notes:
            - will output same flag to indicate noncontiguity, oversaturation and undersaturation.
            - disregarding PQ contiguity flag (see eo_filter instead) to exclude thermal bands.
            - permitting simultaneous flags (through addition syntax) since constants happen to be
              different powers of the same base.
            - dilates the cloud and cloud shadow. (Previous implementation eroded the negation.)
            - input must be numpy not xarray.DataArray (due to depreciated boolean fancy indexing behaviour)
    """
    #ipq = ~pq  # bitwise-not, e.g. flag cloudiness rather than cloudfree

    masking = np.zeros(pq.shape, dtype=np.uint8)
    #masking[(pq & C2_NODATA_BITS).astype(np.bool)] = constants.NO_DATA #retry the no data when it's not inverted
    #masking[(ipq & C2_CLOUD_BITS)] += constants.MASKED_CLOUD
    #masking[(ipq & C2_CLOUD_SHADOW_BITS)] += constants.MASKED_CLOUD_SHADOW #if you take dilate out, need astype bool
    #masking[(pq & C2_CLOUD_BITS).astype(np.bool)] += constants.MASKED_CLOUD
    masking[((~pq & C2_CLEAR_BITS) | (pq & C2_CIRRUS_BITS)).astype(np.bool)] += constants.MASKED_CLOUD
    #masking[((pq & C2_DILATED_BITS) | (pq & C2_CIRRUS_BITS)).astype(np.bool)] += constants.MASKED_CLOUD
    #masking[((pq & C2_DILATED_BITS)).astype(np.bool)] += constants.MASKED_CLOUD
    masking[dilate(pq & C2_CLOUD_SHADOW_BITS)] += constants.MASKED_CLOUD_SHADOW
    return masking

def terrain_filter(dsm, nbar):
    """ Terrain shadow masking, slope masking, solar incidence angle masking.

    Input: xarray DataSets
    """

    shadows, slope, sia = terrain.shadows_and_slope(dsm, nbar.blue.time.values)

    shadowy = dilate(shadows != terrain.LIT)

    low_sia = (sia < constants.LOW_SOLAR_INCIDENCE_THRESHOLD_DEGREES)

    steep = (slope > constants.SLOPE_THRESHOLD_DEGREES)

    result = np.uint8(constants.MASKED_TERRAIN_SHADOW) * shadowy | np.uint8(
        constants.MASKED_HIGH_SLOPE) * steep | np.uint8(constants.MASKED_LOW_SOLAR_ANGLE) * low_sia

    return xarray.DataArray(result, coords=[dsm.y, dsm.x])  # note, assumes (y,x) axis ordering


def eo_filter(source):
    """
    Find where there is no data

    Input must be dataset, not array (since bands could have different nodata values).

    Contiguity can easily be tested either here or using PQ.
    """
    nodata_bools = source.map(lambda array: array == array.nodata).to_array(dim='band')

    nothingness = nodata_bools.all(dim='band')
    noncontiguous = nodata_bools.any(dim='band')

    return np.uint8(constants.NO_DATA) * nothingness | np.uint8(constants.MASKED_NO_CONTIGUITY) * noncontiguous


def fmask_filter(fmask):
    masking = np.zeros(fmask.shape, dtype=np.uint8)
    masking[fmask == 0] += NO_DATA
    masking[fmask == 2] += MASKED_CLOUD
    masking[fmask == 3] += MASKED_CLOUD_SHADOW

    return masking
