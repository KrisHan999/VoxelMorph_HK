"""
tensorflow/keras utilities for the neuron project

If you use this code, please cite 
Dalca AV, Guttag J, Sabuncu MR
Anatomical Priors in Convolutional Networks for Unsupervised Biomedical Segmentation, 
CVPR 2018

or for the transformation/interpolation related functions:

Unsupervised Learning for Fast Probabilistic Diffeomorphic Registration
Adrian V. Dalca, Guha Balakrishnan, John Guttag, Mert R. Sabuncu
MICCAI 2018.

Contact: adalca [at] csail [dot] mit [dot] edu
License: GPLv3
"""

# python imports
import itertools

# third party imports
import numpy as np
from tqdm import tqdm_notebook as tqdm
from pprint import pformat

import pytool.patchlib as pl
import pytool.timer as timer

# local imports
import pynd.ndutils as nd

# often changed file
from imp import reload
import keras
import keras.backend as K
import tensorflow as tf
reload(pl)

def interpn(vol, loc, interp_method='linear'):
    """
    N-D gridded interpolation in tensorflow

    vol can have more dimensions than loc[i], in which case loc[i] acts as a slice 
    for the first dimensions

    Parameters:
        vol: volume with size vol_shape or [*vol_shape, nb_features]
        loc: a N-long list of N-D Tensors (the interpolation locations) for the new grid
            each tensor has to have the same size (but not nec. same size as vol)
            or a tensor of size [*new_vol_shape, N]
        interp_method: interpolation type 'linear' (default) or 'nearest'

    Returns:
        new interpolated volume of the same size as the entries in loc

    TODO:
        enable optional orig_grid - the original grid points.
        check out tf.contrib.resampler, only seems to work for 2D data
    """

    # vol.shape = [160, 192, 224, 1]
    # loc.shape = [3, 160, 192, 224]
    # interp_method = 'linear'
    if isinstance(loc, (list, tuple)):
        loc = tf.stack(loc, -1)   # change loc's shape to [160, 192, 224, 3]

    # extract and check sizes and dimensions
    new_volshape = loc.shape[:-1]  # -> [160, 192, 224]
    nb_dims = len(new_volshape)  # -> 3

    # print("loc shape: ")
    # print(loc.shape)  (160, 192, 224, 3)
    # print("nb_dims: " + str(nb_dims))

    if nb_dims != loc.shape[-1]:
        raise Exception("Number of loc Tensors %d does not match volume dimension %d"
                        % (loc.shape[-1], nb_dims))

    if nb_dims > len(vol.shape):
        raise Exception("Loc dimension %d does not match volume dimension %d"
                        % (nb_dims, len(vol.shape)))

    if len(vol.shape) == nb_dims:
        vol = K.expand_dims(vol, -1)

    # flatten and float location Tensors
    loc = tf.cast(loc, 'float32')



    # interpolate
    if interp_method == 'linear':
        loc0 = tf.floor(loc)
        # loc0 is the floor value of loc

        # clip values
        max_loc = [d - 1 for d in vol.get_shape().as_list()]
        # max_loc = [159, 191, 223]

        loc0 = [tf.clip_by_value(loc0[...,d], 0, max_loc[d]) for d in range(nb_dims)]
        # loc0 is the clipped value between 0 and max_loc, shape=[3, 160, 192, 224]

        # get other end of point cube
        loc1 = [tf.clip_by_value(loc0[d] + 1, 0, max_loc[d]) for d in range(nb_dims)]
        # loc1 is the ceiling value of loc, clipped by 0 and max_loc, shape=[3, 160, 192, 224]

        locs = [[tf.cast(f, 'int32') for f in loc0], [tf.cast(f, 'int32') for f in loc1]]
        # locs = [loc0, loc1], shape=[2, 3, 160, 192, 224]


        # compute the difference between the upper value and the original value
        # differences are basically 1 - (pt - floor(pt))
        #   because: floor(pt) + 1 - pt = 1 + (floor(pt) - pt) = 1 - (pt - floor(pt))

        diff_loc1 = [loc1[d] - loc[...,d] for d in range(nb_dims)]
        # diff_loc1 is the difference between upper value and the original value, shape=[3, 160, 192, 224]

        diff_loc0 = [1 - d for d in diff_loc1]
        # diff_loc0 is the difference between original value and the lower value, shape=[3, 160, 192, 224]

        weights_loc = [diff_loc1, diff_loc0] # note reverse ordering since weights are inverse of diff. shape=[2, 3, 160, 192, 224]
        # 注意这里是先diff_loc1, 然后diff_loc0. 是因为diff_loc1正相关于取floor的概率，diff_loc0正想关于取ceil的概率

        # go through all the cube corners, indexed by a ND binary vector 
        # e.g. [0, 0] means this "first" corner in a 2-D "cube"
        cube_pts = list(itertools.product([0, 1], repeat=nb_dims))
        # 得到八个相邻点, 0表示floor, 1表示ceil。 return [(0, 0, 0), (0, 0, 1), (0, 1, 0), (0, 1, 1), (1, 0, 0), (1, 0, 1), (1, 1, 0), (1, 1, 1)]
        interp_vol = 0
        # print(cube_pts)
        
        for c in cube_pts:
            
            # get nd values
            # note re: indices above volumes via https://github.com/tensorflow/tensorflow/issues/15091
            #   It works on GPU because we do not perform index validation checking on GPU -- it's too
            #   expensive. Instead we fill the output with zero for the corresponding value. The CPU
            #   version caught the bad index and returned the appropriate error.
            subs = [locs[c[d]][d] for d in range(nb_dims)]
            # subs表示都取c对应方向的偏移量
            # sub.shape = [3, 160, 192, 224] for e.g. c=[0,0,0], then subs is loc

            # tf stacking is slow for large volumes, so we will use sub2ind and use single indexing.
            # indices = tf.stack(subs, axis=-1)
            # vol_val = tf.gather_nd(vol, indices)
            # faster way to gather than gather_nd, because the latter needs tf.stack which is slow :(
            idx = sub2ind(vol.shape[:-1], subs)
            # idx的每一个元素对应一个位置， 确定index
            # print(idx.shape) shape=[160, 192, 224]

            _vol = tf.reshape(vol, [-1, vol.shape[-1]])
            # print(_vol.shape) shape=(6881280, 1)  6881280 = 160*192*224
            vol_val = tf.gather(_vol, idx)
            # 用idx拉确定在_vol中的位置->indexs，_vol.shape = (6881280, 1), idx的每一个元素对应_vol中的一个值
            # vol_val[0][0][0] = _vol[idx[0][0][0]], 获得转换后的vol值

            # print(vol_val.shape) shape=[160, 192, 224, 1]

            # get the weight of this cube_pt based on the distance
            # if c[d] is 0 --> want weight = 1 - (pt - floor[pt]) = diff_loc1
            # if c[d] is 1 --> want weight = pt - floor[pt] = diff_loc0
            wts_lst = [weights_loc[c[d]][d] for d in range(nb_dims)]
            # shape = [3, 160, 192, 224]
            # tf stacking is slow, we we will use prod_n()
            # wlm = tf.stack(wts_lst, axis=0)
            # wt = tf.reduce_prod(wlm, axis=0)
            wt = prod_n(wts_lst)
            # wt 表明interp_vol受到c对应的vol_val的影响大小。

            wt = K.expand_dims(wt, -1)
            # 按c方向转换的概率
            # wt.shape = [160, 192, 224, 1]
            
            # compute final weighted value for each cube corner
            interp_vol += wt * vol_val
            # wt*vol_vol -> 在c对应方向上vol_val对最终interp_vol的贡献
            # interp_vol -> 八个方向得到的最终输出
        
    else:
        assert interp_method == 'nearest'
        roundloc = tf.cast(tf.round(loc), 'int32')

        # clip values
        max_loc = [tf.cast(d - 1, 'int32') for d in vol.shape]
        roundloc = [tf.clip_by_value(roundloc[...,d], 0, max_loc[d]) for d in range(nb_dims)]

        # get values
        # tf stacking is slow. replace with gather
        # roundloc = tf.stack(roundloc, axis=-1)
        # interp_vol = tf.gather_nd(vol, roundloc)
        idx = sub2ind(vol.shape[:-1], roundloc)
        interp_vol = tf.gather(tf.reshape(vol, [-1, vol.shape[-1]]), idx) 

    return interp_vol


def prod_n(lst):
    # lst.shape = [3, 160, 192, 224]
    prod = lst[0]
    for p in lst[1:]:
        prod *= p
    return prod


def sub2ind(siz, subs, **kwargs):
    """
    assumes column-order major
    """
    # subs is a list
    # siz = [160, 192, 224]
    # subs.shape = [3, 160, 192, 224]
    assert len(siz) == len(subs), 'found inconsistent siz and subs: %d %d' % (len(siz), len(subs))

    k = np.cumprod(siz[::-1])
    # siz[::-1] 由[160, 192, 224]变为[224, 192, 160]，k=[224,43008,6881280]

    # 在上层函数interpn中，我们需要得到idx也就是本函数中的ndx去得到偏移后的vol_val, 在上一层函数中，我们将vol展开得到_vol(shape=[6881280,1])
    # 上层函数中vol_val = tf.gather(_vol, idx)，因此我们需要idx作为索引得到偏移后的vol_val。vol[0][0][0] = _vol[idx[0][0][0]]
    # idx为偏移矩阵，其每个元素idx[i][j][k]都对应一个key，例如：idx[0][0][0]=12, 那么vol[0][0][0]=_vol[12]. 
    # 那么如何得到偏移矩阵。 首先我们看vol是如何展开的，shape=[nx, ny, nz], e.g. [[[1,2],[3,4]][[5,6],[7,8]]] -> shape=(2,2,2),展开时先展Z方向，再展Y方向，最后是X方向。
    # 得到[[1],[2],[3],[4],[5],[6],[7],[8]]，因此我们求偏移矩阵时也要根据这个特点，先是z，再y，最后x


    ndx = subs[-1]
    # ndx.shape=[160, 192, 224], ndx是z方向的shift


    # print(len(subs[:-1][::-1]))
    # print(subs[0][::-1].shape)
    # subs[:-1][::-1] -> size [2, 160, 192, 224]
    # subs[:-1]得到的shape=[2， 160， 192， 224], subs[:-1][0]是x, subs[:-1][1]是y，所以subs[:-1]对应的是[x,y], subs[:-1][::-1]对应的是[y,x]
    
    for i, v in enumerate(subs[:-1][::-1]):
        # 根据上面所说的求偏移矩阵，第一个ndx对应的是z方向，然后添加y方向(224 * y), 最后添加x方向(224*192*x).
        # 举例，subs[:,0,0,0] = [11,13,15], 分别对应x,y,z, 这就是说[0][0][0]点对应的实际是vol中的[11,13,15],因为vol被展开成_vol(shape=[6881280,1])
        # 因此将[11,13,15]转成15+13*224+11*224*192=476015，也就是说ndx[0][0][0]对应的是_vol[476015], --->最后得到偏移矩阵
        ndx = ndx + v * k[i]
    # ndx.shape = [160, 192, 224]
    return ndx


def affine_to_shift(affine_matrix, volshape, shift_center=True, indexing='ij'):
    """
    transform an affine matrix to a dense location shift tensor in tensorflow

    Algorithm:
        - get grid and shift grid to be centered at the center of the image (optionally)
        - apply affine matrix to each index.
        - subtract grid

    Parameters:
        affine_matrix: ND+1 x ND+1 or ND x ND+1 matrix (Tensor)
        volshape: 1xN Nd Tensor of the size of the volume.
        shift_center (optional)

    Returns:
        shift field (Tensor) of size *volshape x N

    TODO: 
        allow affine_matrix to be a vector of size nb_dims * (nb_dims + 1)
    """

    if isinstance(volshape, (tf.Dimension, tf.TensorShape)):
        volshape = volshape.as_list()
    
    if affine_matrix.dtype != 'float32':
        affine_matrix = tf.cast(affine_matrix, 'float32')

    nb_dims = len(volshape)

    if len(affine_matrix.shape) == 1:
        if len(affine_matrix) != (nb_dims * (nb_dims + 1)) :
            raise ValueError('transform is supposed a vector of len ndims * (ndims + 1).'
                             'Got len %d' % len(affine_matrix))

        affine_matrix = tf.reshape(affine_matrix, [nb_dims, nb_dims + 1])

    if not (affine_matrix.shape[0] in [nb_dims, nb_dims + 1] and affine_matrix.shape[1] == (nb_dims + 1)):
        raise Exception('Affine matrix shape should match'
                        '%d+1 x %d+1 or ' % (nb_dims, nb_dims) + \
                        '%d x %d+1.' % (nb_dims, nb_dims) + \
                        'Got: ' + str(volshape))

    # list of volume ndgrid
    # N-long list, each entry of shape volshape
    mesh = volshape_to_meshgrid(volshape, indexing=indexing)  
    mesh = [tf.cast(f, 'float32') for f in mesh]
    
    if shift_center:
        mesh = [mesh[f] - (volshape[f]-1)/2 for f in range(len(volshape))]

    # add an all-ones entry and transform into a large matrix
    flat_mesh = [flatten(f) for f in mesh]
    flat_mesh.append(tf.ones(flat_mesh[0].shape, dtype='float32'))
    mesh_matrix = tf.transpose(tf.stack(flat_mesh, axis=1))  # 4 x nb_voxels

    # compute locations
    loc_matrix = tf.matmul(affine_matrix, mesh_matrix)  # N+1 x nb_voxels
    loc_matrix = tf.transpose(loc_matrix[:nb_dims, :])  # nb_voxels x N
    loc = tf.reshape(loc_matrix, list(volshape) + [nb_dims])  # *volshape x N
    # loc = [loc[..., f] for f in range(nb_dims)]  # N-long list, each entry of shape volshape

    # get shifts and return
    return loc - tf.stack(mesh, axis=nb_dims)


def transform(vol, loc_shift, interp_method='linear', indexing='ij'):
    """
    transform (interpolation N-D volumes (features) given shifts at each location in tensorflow

    Essentially interpolates volume vol at locations determined by loc_shift. 
    This is a spatial transform in the sense that at location [x] we now have the data from, 
    [x + shift] so we've moved data.

    Parameters:
        vol: volume with size vol_shape or [*vol_shape, nb_features], vol.shape = [160, 192, 224, 1]
        loc_shift: shift volume [*new_vol_shape, N],  loc_shift.shape = [160, 192, 224, 3]
        interp_method (default:'linear'): 'linear', 'nearest'
        indexing (default: 'ij'): 'ij' (matrix) or 'xy' (cartesian).
            In general, prefer to leave this 'ij'
    
    Return:
        new interpolated volumes in the same size as loc_shift[0]
    
    Keyworks:
        interpolation, sampler, resampler, linear, bilinear
    """

    # parse shapes
    if isinstance(loc_shift.shape, (tf.Dimension, tf.TensorShape)):
        volshape = loc_shift.shape[:-1].as_list()
    else:
        volshape = loc_shift.shape[:-1]
    # vol_shape = [160, 192, 224]
    nb_dims = len(volshape)  # -> 3

    # location should be mesh and delta
    mesh = volshape_to_meshgrid(volshape, indexing=indexing)  # volume mesh, indexing = 'ij', shape = [3, 160, 192, 224]
    # mesh_shift[0], 数值沿着axis=0递增，每个YZ平面数值相同
    # mesh_shift[1], 数值沿着axis=1递增，每个ZX平面数值相同
    # mesh_shift[2], 数值沿着axis=2递增，每个XY平面数值相同
    loc = [tf.cast(mesh[d], 'float32') + loc_shift[..., d] for d in range(nb_dims)]
    # loc_shift[0], 沿着X方向的偏移
    # loc_shift[1], 沿着Y方向的偏移
    # loc_shift[2], 沿着Z方向的偏移
    # loc.shape = [3, 160, 192, 224]

    # test single
    # interp_method = 'linear'
    return interpn(vol, loc, interp_method=interp_method)


def integrate_vec(vec, time_dep=False, method='ss', **kwargs):
    """
    Integrate (stationary of time-dependent) vector field (N-D Tensor) in tensorflow
    
    Aside from directly using tensorflow's numerical integration odeint(), also implements 
    "scaling and squaring", and quadrature. Note that the diff. equation given to odeint
    is the one used in quadrature.   

    Parameters:
        vec: the Tensor field to integrate. 
            If vol_size is the size of the intrinsic volume, and vol_ndim = len(vol_size),
            then vector shape (vec_shape) should be 
            [vol_size, vol_ndim] (if stationary)
            [vol_size, vol_ndim, nb_time_steps] (if time dependent)
        time_dep: bool whether vector is time dependent
        method: 'scaling_and_squaring' or 'ss' or 'ode' or 'quadrature'
        
        if using 'scaling_and_squaring': currently only supports integrating to time point 1.
            nb_steps: int number of steps. Note that this means the vec field gets broken
            down to 2**nb_steps. so nb_steps of 0 means integral = vec.

        if using 'ode':
            out_time_pt (optional): a time point or list of time points at which to evaluate
                Default: 1
            init (optional): if using 'ode', the initialization method.
                Currently only supporting 'zero'. Default: 'zero'
            ode_args (optional): dictionary of all other parameters for 
                tf.contrib.integrate.odeint()

    Returns:
        int_vec: integral of vector field.
        Same shape as the input if method is 'scaling_and_squaring', 'ss', 'quadrature', 
        or 'ode' with out_time_pt not a list. Will have shape [*vec_shape, len(out_time_pt)]
        if method is 'ode' with out_time_pt being a list.

    Todo:
        quadrature for more than just intrinsically out_time_pt = 1
    """

    if method not in ['ss', 'scaling_and_squaring', 'ode', 'quadrature']:
        raise ValueError("method has to be 'scaling_and_squaring' or 'ode'. found: %s" % method)

    if method in ['ss', 'scaling_and_squaring']:
        nb_steps = kwargs['nb_steps']
        assert nb_steps >= 0, 'nb_steps should be >= 0, found: %d' % nb_steps

        if time_dep:
            svec = K.permute_dimensions(vec, [-1, *range(0, vec.shape[-1] - 1)])
            assert 2**nb_steps == svec.shape[0], "2**nb_steps and vector shape don't match"

            svec = svec/(2**nb_steps)
            for _ in range(nb_steps):
                svec = svec[0::2] + tf.map_fn(transform, svec[1::2,:], svec[0::2,:])

            disp = svec[0, :]

        else:
            vec = vec/(2**nb_steps)
            for _ in range(nb_steps):
                vec += transform(vec, vec)
            disp = vec

    elif method == 'quadrature':
        # TODO: could output more than a single timepoint!
        nb_steps = kwargs['nb_steps']
        assert nb_steps >= 1, 'nb_steps should be >= 1, found: %d' % nb_steps

        vec = vec/nb_steps

        if time_dep:
            disp = vec[...,0]
            for si in range(nb_steps-1):
                disp += transform(vec[...,si+1], disp)
        else:
            disp = vec
            for _ in range(nb_steps-1):
                disp += transform(vec, disp)

    else:
        assert not time_dep, "odeint not implemented with time-dependent vector field"
        fn = lambda disp, _: transform(vec, disp)  

        # process time point.
        out_time_pt = kwargs['out_time_pt'] if 'out_time_pt' in kwargs.keys() else 1
        single_out_time_pt = not isinstance(out_time_pt, (list, tuple))
        if single_out_time_pt: out_time_pt = [out_time_pt]
        K_out_time_pt = K.variable([0, *out_time_pt])

        # process initialization
        if 'init' not in kwargs.keys() or kwargs['init'] == 'zero':
            disp0 = vec*0
        else:
            raise ValueError('non-zero init for ode method not implemented')

        # compute integration with tf.contrib.integrate.odeint
        if 'ode_args' not in kwargs.keys(): kwargs['ode_args'] = {}
        disp = tf.contrib.integrate.odeint(fn, disp0, K_out_time_pt, **kwargs['ode_args'])
        disp = K.permute_dimensions(disp[1:len(out_time_pt)+1, :], [*range(1,len(disp.shape)), 0])

        # return
        if single_out_time_pt: 
            disp = disp[...,0]

    return disp


def volshape_to_ndgrid(volshape, **kwargs):
    """
    compute Tensor ndgrid from a volume size

    Parameters:
        volshape: the volume size
        **args: "name" (optional)

    Returns:
        A list of Tensors

    See Also:
        ndgrid
    """
    
    isint = [float(d).is_integer() for d in volshape]
    if not all(isint):
        raise ValueError("volshape needs to be a list of integers")

    linvec = [tf.range(0, d) for d in volshape]
    return ndgrid(*linvec, **kwargs)


def volshape_to_meshgrid(volshape, **kwargs):
    # volshape = [160, 192, 224]
    # indexing='ij'
    """
    compute Tensor meshgrid from a volume size

    Parameters:
        volshape: the volume size
        **args: "name" (optional)

    Returns:
        A list of Tensors

    See Also:
        tf.meshgrid, meshgrid, ndgrid, volshape_to_ndgrid
    """
    
    isint = [float(d).is_integer() for d in volshape]
    if not all(isint):
        raise ValueError("volshape needs to be a list of integers")

    linvec = [tf.range(0, d) for d in volshape]
    # linvec = [tf.range(160), tf.range(1922), tf.range(224)]
    return meshgrid(*linvec, **kwargs)


def ndgrid(*args, **kwargs):
    """
    broadcast Tensors on an N-D grid with ij indexing
    uses meshgrid with ij indexing

    Parameters:
        *args: Tensors with rank 1
        **args: "name" (optional)

    Returns:
        A list of Tensors
    
    """
    return meshgrid(*args, indexing='ij', **kwargs)


def flatten(v):
    """
    flatten Tensor v
    
    Parameters:
        v: Tensor to be flattened
    
    Returns:
        flat Tensor
    """

    return tf.reshape(v, [-1])


def meshgrid(*args, **kwargs):
    """
    
    meshgrid code that builds on (copies) tensorflow's meshgrid but dramatically
    improves runtime by changing the last step to tiling instead of multiplication.
    https://github.com/tensorflow/tensorflow/blob/c19e29306ce1777456b2dbb3a14f511edf7883a8/tensorflow/python/ops/array_ops.py#L1921
    
    Broadcasts parameters for evaluation on an N-D grid.
    Given N one-dimensional coordinate arrays `*args`, returns a list `outputs`
    of N-D coordinate arrays for evaluating expressions on an N-D grid.
    Notes:
    `meshgrid` supports cartesian ('xy') and matrix ('ij') indexing conventions.
    When the `indexing` argument is set to 'xy' (the default), the broadcasting
    instructions for the first two dimensions are swapped.
    Examples:
    Calling `X, Y = meshgrid(x, y)` with the tensors
    ```python
    x = [1, 2, 3]
    y = [4, 5, 6]
    X, Y = meshgrid(x, y)
    # X = [[1, 2, 3],
    #      [1, 2, 3],
    #      [1, 2, 3]]
    # Y = [[4, 4, 4],
    #      [5, 5, 5],
    #      [6, 6, 6]]
    ```
    Args:
    *args: `Tensor`s with rank 1.
    **kwargs:
      - indexing: Either 'xy' or 'ij' (optional, default: 'xy').
      - name: A name for the operation (optional).
    Returns:
    outputs: A list of N `Tensor`s with rank N.
    Raises:
    TypeError: When no keyword arguments (kwargs) are passed.
    ValueError: When indexing keyword argument is not one of `xy` or `ij`.
    """

    indexing = kwargs.pop("indexing", "xy")
    # indexing = 'ij'
    name = kwargs.pop("name", "meshgrid")
    # name = 'meshigrid'

    if kwargs:
        key = list(kwargs.keys())[0]
        raise TypeError("'{}' is an invalid keyword argument "
                    "for this function".format(key))

    if indexing not in ("xy", "ij"):
        raise ValueError("indexing parameter must be either 'xy' or 'ij'")

    # with ops.name_scope(name, "meshgrid", args) as name:
    # args = [tf.range(160), tf.range(1922), tf.range(224)]
    ndim = len(args)
    s0 = (1,) * ndim # -> (1,1,1)

    # Prepare reshape by inserting dimensions with size 1 where needed
    output = []
    for i, x in enumerate(args):
        output.append(tf.reshape(tf.stack(x), (s0[:i] + (-1,) + s0[i + 1::])))
        # output[0].shape = [160, 1, 1]
        # output[1].shape = [1, 192, 1]
        # output[2].sahpe = [1, 1, 224]
    # Create parameters for broadcasting each tensor to the full size
    shapes = [tf.size(x) for x in args]
    # print(shapes)  ?? 好像没用
    sz = [x.get_shape().as_list()[0] for x in args]
    # print(sz) -> [160, 192, 224]

    # output_dtype = tf.convert_to_tensor(args[0]).dtype.base_dtype

    if indexing == "xy" and ndim > 1:
        output[0] = tf.reshape(output[0], (1, -1) + (1,) * (ndim - 2))
        output[1] = tf.reshape(output[1], (-1, 1) + (1,) * (ndim - 2))
        shapes[0], shapes[1] = shapes[1], shapes[0]
        sz[0], sz[1] = sz[1], sz[0]

    # This is the part of the implementation from tf that is slow. 
    # We replace it below to get a ~6x speedup (essentially using tile instead of * tf.ones())
    # TODO(nolivia): improve performance with a broadcast  
    # mult_fact = tf.ones(shapes, output_dtype)
    # return [x * mult_fact for x in output]
    for i in range(len(output)):       
        output[i] = tf.tile(output[i], tf.stack([*sz[:i], 1, *sz[(i+1):]]))
        # indexing = 'ij'
        # output[0] -> shape=[160, 192, 224], 数值沿着axis=0递增，每个YZ平面数值相同
        # output[1] -> shape=[160, 192, 224], 数值沿着axis=1递增，每个XZ平面数值相同
        # output[2] -> shape=[160, 192, 224], 数值沿着axis=2递增，每个XY平面数值相同
    return output
    


def gaussian_kernel(sigma, windowsize=None, indexing='ij'):
    """
    sigma will be a number of a list of numbers.

    # some guidance from my MATLAB file 
    https://github.com/adalca/mivt/blob/master/src/gaussFilt.m

    Parameters:
        sigma: scalar or list of scalars
        windowsize (optional): scalar or list of scalars indicating the shape of the kernel
    
    Returns:
        ND kernel the same dimensiosn as the number of sigmas.

    Todo: could use MultivariateNormalDiag
    """

    if not isinstance(sigma, (list, tuple)):
        sigma = [sigma]
    sigma = [np.maximum(f, np.finfo(float).eps) for f in sigma]

    nb_dims = len(sigma)

    # compute windowsize
    if windowsize is None:
        windowsize = [np.round(f * 3) * 2 + 1 for f in sigma]

    if len(sigma) != len(windowsize):
        raise ValueError('sigma and windowsize should have the same length.'
                         'Got vectors: ' + str(sigma) + 'and' + str(windowsize))

    # ok, let's get to work.
    mid = [(w - 1)/2 for w in windowsize]

    # list of volume ndgrid
    # N-long list, each entry of shape volshape
    mesh = volshape_to_meshgrid(windowsize, indexing=indexing)  
    mesh = [tf.cast(f, 'float32') for f in mesh]

    # compute independent gaussians
    diff = [mesh[f] - mid[f] for f in range(len(windowsize))]
    exp_term = [- K.square(diff[f])/(2 * (sigma[f]**2)) for f in range(nb_dims)]
    norms = [exp_term[f] - np.log(sigma[f] * np.sqrt(2 * np.pi)) for f in range(nb_dims)]

    # add an all-ones entry and transform into a large matrix
    norms_matrix = tf.stack(norms, axis=-1)  # *volshape x N
    g = K.sum(norms_matrix, -1)  # volshape
    g = tf.exp(g)
    g /= tf.reduce_sum(g)

    return g





def stack_models(models, connecting_node_ids=None):
    """
    stacks keras models sequentially without nesting the models into layers
        (the nominal behaviour in keras as of 1/13/2018 is to nest models)
    This preserves the layers (i.e. does not copy layers). This means that if you modify the
    original layer weights, you are automatically affecting the new stacked model.

    Parameters:
        models: a list of models, in order of: [input_model, second_model, ..., final_output_model]
        connecting_node_ids (optional): a list of connecting node pointers from Nth model to N+1th model

    Returns:
        new stacked model pointer
    """

    output_tensors = models[0].outputs
    stacked_inputs = [*models[0].inputs]

    # go through models 1 onwards and stack with current graph
    for mi in range(1, len(models)):
        
        # prepare input nodes - a combination of 
        new_input_nodes = list(models[mi].inputs)
        stacked_inputs_contrib = list(models[mi].inputs)

        if connecting_node_ids is None: 
            conn_id = list(range(len(new_input_nodes)))
            assert len(new_input_nodes) == len(models[mi-1].outputs), \
                'argument count does not match'
        else:
            conn_id = connecting_node_ids[mi-1]

        for out_idx, ii in enumerate(conn_id):
            new_input_nodes[ii] = output_tensors[out_idx]
            stacked_inputs_contrib[ii] = None
        
        output_tensors = mod_submodel(models[mi], new_input_nodes=new_input_nodes)
        stacked_inputs = stacked_inputs + stacked_inputs_contrib

    stacked_inputs = [i for i in stacked_inputs if i is not None]
    new_model = keras.models.Model(stacked_inputs, output_tensors)
    return new_model


def mod_submodel(orig_model,
                 new_input_nodes=None,
                 input_layers=None):
    """
    modify (cut and/or stitch) keras submodel

    layer objects themselved will be untouched - the new model, even if it includes, 
    say, a subset of the previous layers, those layer objects will be shared with
    the original model

    given an original model:
        model stitching: given new input node(s), get output tensors of having pushed these 
        nodes through the model
        
        model cutting: given input layer (pointers) inside the model, the new input nodes
        will match the new input layers, hence allowing cutting the model

    Parameters:
        orig_model: original keras model pointer
        new_input_nodes: a pointer to a new input node replacement
        input_layers: the name of the layer in the original model to replace input nodes
    
    Returns:
        pointer to modified model
    """

    def _layer_dependency_dict(orig_model):
        """
        output: a dictionary of all layers in the orig_model
        for each layer:
            dct[layer] is a list of lists of layers.
        """

        out_layers = orig_model.output_layers
        out_node_idx = orig_model.output_layers_node_indices

        node_list = [ol._inbound_nodes[out_node_idx[i]] for i, ol in enumerate(out_layers)]
            
        dct = {}
        dct_node_idx = {}
        while len(node_list) > 0:
            node = node_list.pop(0)
                
            add = True
            # if not empty. we need to check that we're not adding the same layers through the same node.
            if len(dct.setdefault(node.outbound_layer, [])) > 0:
                for li, layers in enumerate(dct[node.outbound_layer]):
                    if layers == node.inbound_layers and \
                        dct_node_idx[node.outbound_layer][li] == node.node_indices:
                        add = False
                        break
            if add:
                dct[node.outbound_layer].append(node.inbound_layers)
                dct_node_idx.setdefault(node.outbound_layer, []).append(node.node_indices)
                #print(node, node.outbound_layer)
            # append is in place

            # add new node
            for li, layer in enumerate(node.inbound_layers):
                if hasattr(layer, '_inbound_nodes'):
                    node_list.append(layer._inbound_nodes[node.node_indices[li]])

        return dct

    def _get_new_layer_output(layer, new_layer_outputs, inp_layers):
        """
        (recursive) given a layer, get new outbound_nodes based on new inbound_nodes

        new_layer_outputs is a (reference) dictionary that we will be adding
        to within the recursion stack.
        """

        if layer not in new_layer_outputs:

            if layer not in inp_layers:
                raise Exception('layer %s is not in inp_layers' % layer.name)

            # for all input layers to this layer, gather their output (our input)
            for group in inp_layers[layer]:
                input_nodes = [None] * len(group)
                for li, inp_layer in enumerate(group):
                    if inp_layer in new_layer_outputs:
                        input_nodes[li] = new_layer_outputs[inp_layer]
                    else: # recursive call
                        input_nodes[li] = _get_new_layer_output(inp_layer, new_layer_outputs, inp_layers)

                # layer call
                if len(input_nodes) == 1:
                    new_layer_outputs[layer] = layer(*input_nodes)
                else:
                    new_layer_outputs[layer] = layer(input_nodes)

        return new_layer_outputs[layer]



    # for each layer create list of input layers
    inp_layers = _layer_dependency_dict(orig_model)

    # get input layers
    #   These layers will be 'ignored' in that they will not be called!
    #   instead, the outbound nodes of the layers will be the input nodes
    #   computed below or passed in
    if input_layers is None: # if none provided, search for them
        InputLayerClass = keras.engine.topology.InputLayer
        input_layers = [l for l in orig_model.layers if isinstance(l, InputLayerClass)]

    else:
        if not isinstance(input_layers, (tuple, list)):
            input_layers = [input_layers]
        for idx, input_layer in enumerate(input_layers):
            # if it's a string, assume it's layer name, and get the layer pointer
            if isinstance(input_layer, str):
                input_layers[idx] = orig_model.get_layer(input_layer)

    # process new input nodes
    if new_input_nodes is None:
        input_nodes = list(orig_model.inputs)
    else:
        input_nodes = new_input_nodes
    assert len(input_nodes) == len(input_layers)

    # initialize dictionary of layer:new_output_node
    #   note: the input layers are not called, instead their outbound nodes
    #   are assumed to be the given input nodes. If we call the nodes, we can run
    #   into multiple-inbound-nodes problems, or if we completely skip the layers altogether
    #   we have problems with multiple inbound input layers into subsequent layers
    new_layer_outputs = {}
    for i, input_layer in enumerate(input_layers):
        new_layer_outputs[input_layer] = input_nodes[i]

    # recursively go back from output layers and request new input nodes
    output_layers = []
    for layer in orig_model.layers:
        if hasattr(layer, '_inbound_nodes'):
            for i in range(len(layer._inbound_nodes)):
                if layer.get_output_at(i) in orig_model.outputs:
                    output_layers.append(layer)
                    break
    assert len(output_layers) == len(orig_model.outputs), "Number of output layers don't match"

    outputs = [None] * len(output_layers)
    for li, output_layer in enumerate(output_layers):
        outputs[li] = _get_new_layer_output(output_layer, new_layer_outputs, inp_layers)

    return outputs


def reset_weights(model, session=None):
    """
    reset weights of model with the appropriate initializer.
    Note: only uses "kernel_initializer" and "bias_initializer"
    does not close session.

    Reference:
    https://www.codementor.io/nitinsurya/how-to-re-initialize-keras-model-weights-et41zre2g

    Parameters:
        model: keras model to reset
        session (optional): the current session
    """

    if session is None:
        session = K.get_session()

    for layer in model.layers: 
        reset = False
        if hasattr(layer, 'kernel_initializer'):
            layer.kernel.initializer.run(session=session)
            reset = True
        
        if hasattr(layer, 'bias_initializer'):
            layer.bias.initializer.run(session=session)
            reset = True
        
        if not reset:
            print('Could not find initializer for layer %s. skipping', layer.name)


def copy_model_weights(src_model, dst_model):
    """
    copy weights from the src keras model to the dst keras model via layer names

    Parameters:
        src_model: source keras model to copy from
        dst_model: destination keras model to copy to
    """

    for layer in tqdm(dst_model.layers):
        try:
            wts = src_model.get_layer(layer.name).get_weights()
            layer.set_weights(wts)
        except:
            print('Could not copy weights of %s' % layer.name)
            continue


def robust_multi_gpu_model(model, gpus, verbose=True):
    """
    re-work keras model for multi-gpus if number of gpus is > 1

    Parameters:
        model: keras Model
        gpus: list of gpus to split to (e.g. [1, 4, 6]), or count of gpus available (e.g. 3)
            Note: if given int, assume that is the count of gpus, 
            so if you want a single specific gpu, this function will not do that.
        verbose: whether to display what happened (default: True)
    
    Returns:
        keras model
    """

    islist = isinstance(gpus, (list, tuple))
    if (islist and len(gpus) > 1) or (not islist and gpus > 1):
        count = gpus if not islist else len(gpus)
        print("Returning multi-gpu (%d) model" % count)
        return keras.utils.multi_gpu_model(model, count)

    else:
        print("Returning keras model back (single gpu found)")
        return model




def logtanh(x, a=1):
    """
    log * tanh

    See Also: arcsinh
    """
    return K.tanh(x) *  K.log(2 + a * abs(x))


def arcsinh(x, alpha=1):
    """
    asignh

    See Also: logtanh
    """
    return tf.asinh(x * alpha) / alpha







def predict_volumes(models,
                    data_generator,
                    batch_size,
                    patch_size,
                    patch_stride,
                    grid_size,
                    nan_func=np.nanmedian,
                    do_extra_vol=False,  # should compute vols beyond label
                    do_prob_of_true=False,  # should compute prob_of_true vols
                    verbose=False):
    """
    Note: we allow models to be a list or a single model.
    Normally, if you'd like to run a function over a list for some param,
    you can simply loop outside of the function. here, however, we are dealing with a generator,
    and want the output of that generator to be consistent for each model.

    Returns:
    if models isa list of more than one model:
        a tuple of model entried, each entry is a tuple of:
        true_label, pred_label, <vol>, <prior_label>, <pred_prob_of_true>, <prior_prob_of_true>
    if models is just one model:
        a tuple of
        (true_label, pred_label, <vol>, <prior_label>, <pred_prob_of_true>, <prior_prob_of_true>)

    TODO: could add prior
    """

    if not isinstance(models, (list, tuple)):
        models = (models,)

    # get the input and prediction stack
    with timer.Timer('predict_volume_stack', verbose):
        vol_stack = predict_volume_stack(models,
                                         data_generator,
                                         batch_size,
                                         grid_size,
                                         verbose)
    if len(models) == 1:
        do_prior = len(vol_stack) == 4
    else:
        do_prior = len(vol_stack[0]) == 4

    # go through models and volumes
    ret = ()
    for midx, _ in enumerate(models):

        stack = vol_stack if len(models) == 1 else vol_stack[midx]

        if do_prior:
            all_true, all_pred, all_vol, all_prior = stack
        else:
            all_true, all_pred, all_vol = stack

        # get max labels
        all_true_label, all_pred_label = pred_to_label(all_true, all_pred)

        # quilt volumes and aggregate overlapping patches, if any
        args = [patch_size, grid_size, patch_stride]
        label_kwargs = {'nan_func_layers':nan_func, 'nan_func_K':nan_func, 'verbose':verbose}
        vol_true_label = _quilt(all_true_label, *args, **label_kwargs).astype('int')
        vol_pred_label = _quilt(all_pred_label, *args, **label_kwargs).astype('int')

        ret_set = (vol_true_label, vol_pred_label)

        if do_extra_vol:
            vol_input = _quilt(all_vol, *args)
            ret_set += (vol_input, )

            if do_prior:
                all_prior_label, = pred_to_label(all_prior)
                vol_prior_label = _quilt(all_prior_label, *args, **label_kwargs).astype('int')
                ret_set += (vol_prior_label, )

        # compute the probability of prediction and prior
        # instead of quilting the probabilistic volumes and then computing the probability
        # of true label, which takes a long time, we'll first compute the probability of label,
        # and then quilt. This is faster, but we'll need to take median votes
        if do_extra_vol and do_prob_of_true:
            all_pp = prob_of_label(all_pred, all_true_label)
            pred_prob_of_true = _quilt(all_pp, *args, **label_kwargs)
            ret_set += (pred_prob_of_true, )

            if do_prior:
                all_pp = prob_of_label(all_prior, all_true_label)
                prior_prob_of_true = _quilt(all_pp, *args, **label_kwargs)

                ret_set += (prior_prob_of_true, )

        ret += (ret_set, )

    if len(models) == 1:
        ret = ret[0]

    # return
    return ret


def predict_volume_stack(models,
                         data_generator,
                         batch_size,
                         grid_size,
                         verbose=False):
    """
    predict all the patches in a volume

    requires batch_size to be a divisor of the number of patches (prod(grid_size))

    Note: we allow models to be a list or a single model.
    Normally, if you'd like to run a function over a list for some param,
    you can simply loop outside of the function. here, however, we are dealing with a generator,
    and want the output of that generator to be consistent for each model.

    Returns:
    if models isa list of more than one model:
        a tuple of model entried, each entry is a tuple of:
        all_true, all_pred, all_vol, <all_prior>
    if models is just one model:
        a tuple of
        all_true, all_pred, all_vol, <all_prior>
    """

    if not isinstance(models, (list, tuple)):
        models = (models,)

    # compute the number of batches we need for one volume
    # we need the batch_size to be a divisor of nb_patches,
    # in order to loop through batches and form full volumes
    nb_patches = np.prod(grid_size)
    # assert np.mod(nb_patches, batch_size) == 0, \
        # "batch_size %d should be a divisor of nb_patches %d" %(batch_size, nb_patches)
    nb_batches = ((nb_patches - 1) // batch_size) + 1

    # go through the patches
    batch_gen = tqdm(range(nb_batches)) if verbose else range(nb_batches)
    for batch_idx in batch_gen:
        sample = next(data_generator)
        nb_vox = np.prod(sample[1].shape[1:-1])
        do_prior = isinstance(sample[0], (list, tuple))

        # pre-allocate all the data
        if batch_idx == 0:
            nb_labels = sample[1].shape[-1]
            all_vol = [np.zeros((nb_patches, nb_vox)) for f in models]
            all_true = [np.zeros((nb_patches, nb_vox * nb_labels)) for f in models]
            all_pred = [np.zeros((nb_patches, nb_vox * nb_labels)) for f in models]
            all_prior = [np.zeros((nb_patches, nb_vox * nb_labels)) for f in models]

        # get in_vol, y_true, y_pred
        for idx, model in enumerate(models):
            # with timer.Timer('prediction', verbose):
            pred = model.predict(sample[0])
            assert pred.shape[0] == batch_size, \
                "batch size mismatch. sample has batch size %d, given batch size is %d" %(pred.shape[0], batch_size)
            input_batch = sample[0] if not do_prior else sample[0][0]

            # compute batch range
            batch_start = batch_idx * batch_size
            batch_end = np.minimum(batch_start + batch_size, nb_patches)
            batch_range = np.arange(batch_start, batch_end)
            batch_vox_idx = batch_end-batch_start

            # update stacks
            all_vol[idx][batch_range, :] = K.batch_flatten(input_batch)[0:batch_vox_idx, :]
            all_true[idx][batch_range, :] = K.batch_flatten(sample[1])[0:batch_vox_idx, :]
            all_pred[idx][batch_range, :] = K._batch_flatten(pred)[0:batch_vox_idx, :]
            if do_prior:
                all_prior[idx][batch_range, :] = K.batch_flatten(sample[0][1])[0:batch_vox_idx, :]

    # reshape probabilistic answers
    for idx, _ in enumerate(models):
        all_true[idx] = np.reshape(all_true[idx], [nb_patches, nb_vox, nb_labels])
        all_pred[idx] = np.reshape(all_pred[idx], [nb_patches, nb_vox, nb_labels])
        if do_prior:
            all_prior[idx] = np.reshape(all_prior[idx], [nb_patches, nb_vox, nb_labels])

    # prepare output tuple
    ret = ()
    for midx, _ in enumerate(models):
        if do_prior:
            ret += ((all_true[midx], all_pred[midx], all_vol[midx], all_prior[midx]), )
        else:
            ret += ((all_true[midx], all_pred[midx], all_vol[midx]), )

    if len(models) == 1:
        ret = ret[0]
    return ret


def prob_of_label(vol, labelvol):
    """
    compute the probability of the labels in labelvol in each of the volumes in vols

    Parameters:
        vol (float numpy array of dim (nd + 1): volume with a prob dist at each voxel in a nd vols
        labelvol (int numpy array of dim nd): nd volume of labels

    Returns:
        nd volume of probabilities
    """

    # check dimensions
    nb_dims = np.ndim(labelvol)
    assert np.ndim(vol) == nb_dims + 1, "vol dimensions do not match [%d] vs [%d]" % (np.ndim(vol)-1, nb_dims)
    shp = vol.shape
    nb_voxels = np.prod(shp[0:nb_dims])
    nb_labels = shp[-1]

    # reshape volume to be [nb_voxels, nb_labels]
    flat_vol = np.reshape(vol, (nb_voxels, nb_labels))

    # normalize accross second dimension
    rows_sums = flat_vol.sum(axis=1)
    flat_vol_norm = flat_vol / rows_sums[:, np.newaxis]

    # index into the flattened volume
    idx = list(range(nb_voxels))
    v = flat_vol_norm[idx, labelvol.flat]
    return np.reshape(v, labelvol.shape)


def next_pred_label(model, data_generator, verbose=False):
    """
    predict the next sample batch from the generator, and compute max labels
    return sample, prediction, max_labels
    """
    sample = next(data_generator)
    with timer.Timer('prediction', verbose):
        pred = model.predict(sample[0])
    sample_input = sample[0] if not isinstance(sample[0], (list, tuple)) else sample[0][0]
    max_labels = pred_to_label(sample_input, pred)
    return (sample, pred) + max_labels

def next_label(model, data_generator):
    """
    predict the next sample batch from the generator, and compute max labels
    return max_labels
    """
    batch_proc = next_pred_label(model, data_generator)
    return (batch_proc[2], batch_proc[3])

def sample_to_label(model, sample):
    """
    redict a sample batch and compute max labels
    return max_labels
    """
    # predict output for a new sample
    res = model.predict(sample[0])
    # return
    return pred_to_label(sample[1], res)

def pred_to_label(*y):
    """
    return the true and predicted labels given true and predicted nD+1 volumes
    """
    # compute resulting volume(s)
    return tuple(np.argmax(f, -1).astype(int) for f in y)

def next_vol_pred(model, data_generator, verbose=False):
    """
    get the next batch, predict model output

    returns (input_vol, y_true, y_pred, <prior>)
    """

    # batch to input, output and prediction
    sample = next(data_generator)
    with timer.Timer('prediction', verbose):
        pred = model.predict(sample[0])
    data = (sample[0], sample[1], pred)
    if isinstance(sample[0], (list, tuple)):  # if given prior, might be a list
        data = (sample[0][0], sample[1], pred, sample[0][1])

    return data





###############################################################################
# functions from some external source
###############################################################################

def batch_gather(reference, indices):
    """
    C+P From Keras pull request https://github.com/keras-team/keras/pull/6377/files
    
    Batchwise gathering of row indices.

    The numpy equivalent is `reference[np.arange(batch_size), indices]`, where
    `batch_size` is the first dimension of the reference tensor.

    # Arguments
        reference: A tensor with ndim >= 2 of shape.
          (batch_size, dim1, dim2, ..., dimN)
        indices: A 1d integer tensor of shape (batch_size) satisfying
          0 <= i < dim2 for each element i.

    # Returns
        The selected tensor with shape (batch_size, dim2, ..., dimN).

    # Examples
        1. If reference is `[[3, 5, 7], [11, 13, 17]]` and indices is `[2, 1]`
        then the result is `[7, 13]`.

        2. If reference is
        ```
          [[[2, 3], [4, 5], [6, 7]],
           [[10, 11], [12, 13], [16, 17]]]
        ```
        and indices is `[2, 1]` then the result is `[[6, 7], [12, 13]]`.
    """
    batch_size = K.shape(reference)[0]
    indices = tf.stack([tf.range(batch_size), indices], axis=1)
    return tf.gather_nd(reference, indices)


###############################################################################
# helper functions
###############################################################################

def _concat(lists, dim):
    if lists[0].size == 0:
        lists = lists[1:]

    return np.concatenate(lists, dim)

def _quilt(patches, patch_size, grid_size, patch_stride, verbose=False, **kwargs):
    assert len(patches.shape) >= 2, "patches has bad shape %s" % pformat(patches.shape)

    # reshape to be [nb_patches x nb_vox]
    patches = np.reshape(patches, (patches.shape[0], -1, 1))

    # quilt
    quilted_vol = pl.quilt(patches, patch_size, grid_size, patch_stride=patch_stride, **kwargs)
    assert quilted_vol.ndim == len(patch_size), "problem with dimensions after quilt"

    # return
    return quilted_vol


# TO MOVE (numpy softmax)
def softmax(x, axis):
    """
    softmax of a numpy array along a given dimension
    """

    return np.exp(x) / np.sum(np.exp(x), axis=axis, keepdims=True)
