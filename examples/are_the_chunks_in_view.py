import itertools
import logging
import sys

import dask.array as da
import numpy as np
import pandas as pd
import toolz as tz
from cachey import Cache

# from https://github.com/janelia-cosem/fibsem-tools
#   pip install fibsem-tools
from fibsem_tools.io import read_xarray
from ome_zarr.io import parse_url
from ome_zarr.reader import Reader
from psygnal import debounced
from scipy.spatial.transform import Rotation as R

import napari

LOGGER = logging.getLogger("poor-mans-octree")
LOGGER.setLevel(logging.DEBUG)

streamHandler = logging.StreamHandler(sys.stdout)
formatter = logging.Formatter(
    '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
streamHandler.setFormatter(formatter)
LOGGER.addHandler(streamHandler)


# A ChunkCacheManager manages multiple chunk caches
class ChunkCacheManager:
    def __init__(self, cache_size=1e9, cost_cutoff=0):
        """
        cache_size, size of cache in bytes
        cost_cutoff, cutoff anything with cost_cutoff or less
        """
        self.c = Cache(cache_size, cost_cutoff)

    def put(self, container, dataset, chunk_slice, value, cost=1):
        """Associate value with key in the given container.
        Container might be a zarr/dataset, key is a chunk_slice, and
        value is the chunk itself.
        """
        k = self.get_container_key(container, dataset, chunk_slice)
        self.c.put(k, value, cost=cost)

    def get_container_key(self, container, dataset, chunk_slice):
        """Create a key from container, dataset, and chunk_slice

        Parameters
        ----------
        container : str
            A string representing a zarr container
        dataset : str
            A string representing a dataset inside a zarr
        chunk_slice : slice
            A ND slice for the chunk to grab

        """
        slice_key = ",".join(
            [f"{st.start}:{st.stop}:{st.step}" for st in chunk_slice]
        )
        return f"{container}/{dataset}@({slice_key})"

    def get(self, container, dataset, chunk_slice):
        """Get a chunk associated with the container, dataset, and chunk_size

        Parameters
        ----------
        container : str
            A string represening a zarr container
        dataset : str
            A string representing a dataset inside the container
        chunk_slice : slice
            A ND slice for the chunk to grab

        """
        return self.c.get(
            self.get_container_key(container, dataset, chunk_slice)
        )


def chunk_centers(array: da.Array):
    """Make a dictionary mapping chunk centers to chunk slices.
    Note: if array is >3D, then the last 3 dimensions are assumed as ZYX
    and will be used for calculating centers


    Parameters
    ----------
    array: dask Array
        The input array.

    Returns
    -------
    chunk_map : dict {tuple of float: tuple of slices}
        A dictionary mapping chunk centers to chunk slices.
    """
    start_pos = [np.cumsum(sizes) - sizes for sizes in array.chunks]
    middle_pos = [
        np.cumsum(sizes) - (np.array(sizes) / 2) for sizes in array.chunks
    ]
    end_pos = [np.cumsum(sizes) for sizes in array.chunks]
    all_start_pos = list(itertools.product(*start_pos))
    # We impose 3D here
    all_middle_pos = [el[-3:] for el in list(itertools.product(*middle_pos))]
    all_end_pos = list(itertools.product(*end_pos))
    chunk_slices = []
    for start, end in zip(all_start_pos, all_end_pos):
        chunk_slice = [
            slice(start_i, end_i) for start_i, end_i in zip(start, end)
        ]
        # We impose 3D here
        chunk_slices.append(tuple(chunk_slice[-3:]))

    mapping = dict(zip(all_middle_pos, chunk_slices))
    return mapping


def rotation_matrix_from_camera(
    camera: napari.components.Camera,
) -> np.ndarray:
    return R.from_euler(seq='yzx', angles=camera.angles, degrees=True)


def visual_depth(points, camera):
    """Compute visual depth from camera position to a(n array of) point(s).

    Parameters
    ----------
    points: (N, D) array of float
        An array of N points. This can be one point or many thanks to NumPy
        broadcasting.
    camera: napari.components.Camera
        A camera model specifying a view direction and a center or focus point.

    Returns
    -------
    projected_length : (N,) array of float
        Position of the points along the view vector of the camera. These can
        be negative (in front of the center) or positive (behind the center).
    """
    view_direction = camera.view_direction
    points_relative_to_camera = points - camera.center
    projected_length = points_relative_to_camera @ view_direction
    return projected_length


def distance_from_camera_centre_line(points, camera):
    """Compute distance from a point or array of points to camera center line.

    This is the line aligned to the camera view direction and passing through
    the camera's center point, aka camera.position.

    Parameters
    ----------
    points: (N, D) array of float
        An array of N points. This can be one point or many thanks to NumPy
        broadcasting.
    camera: napari.components.Camera
        A camera model specifying a view direction and a center or focus point.

    Returns
    -------
    distances : (N,) array of float
        Distances from points to the center line of the camera.
    """
    view_direction = camera.view_direction
    projected_length = visual_depth(points, camera)
    projected = view_direction * np.reshape(projected_length, (-1, 1))
    points_relative_to_camera = (
        points - camera.center
    )  # for performance, don't compute this twice in both functions
    distances = np.linalg.norm(projected - points_relative_to_camera, axis=-1)
    return distances


def prioritised_chunk_loading(depth, distance, zoom, alpha=1.0, visible=None):
    """Compute a chunk priority based on chunk location relative to camera.
    Lower priority is preferred.

    Parameters
    ----------
    depth : (N,) array of float
        The visual depth of the points.
    distance : (N,) array of float
        The distance from the camera centerline of each point.
    zoom : float
        The camera zoom level. The higher the zoom (magnification), the
        higher the relative importance of the distance from the centerline.
    alpha : float
        Parameter weighing distance from centerline and depth. Higher alpha
        means centerline distance is weighted more heavily.
    visible : (N,) array of bool
        An array that indicates the visibility of each chunk

    Returns
    -------
    priority : (N,) array of float
        The loading priority of each chunk.
    """
    chunk_load_priority = depth + alpha * zoom * distance
    if visible is not None:
        chunk_load_priority[np.logical_not(visible)] = np.inf
    return chunk_load_priority


# @tz.curry
# def update_point_colors(event, viewer, alpha=1.0):
#     """Update the points based on their distance to current camera.

#     Parameters:
#     -----------
#     viewer : napari.Viewer
#         Current viewer
#     event : camera.events.angles event
#         The event triggered by changing the camera angles
#     """
#     # TODO we need a grid for each scale, or the grid needs to include all scales
#     points_layer = viewer.layers['grid']
#     points = points_layer.data
#     distances = distance_from_camera_centre_line(points, viewer.camera)
#     depth = visual_depth(points, viewer.camera)
#     priorities = prioritised_chunk_loading(
#         depth, distances, viewer.camera.zoom, alpha=alpha
#     )
#     points_layer.features = pd.DataFrame(
#         {'distance': distances, 'depth': depth, 'priority': priorities}
#     )
#     # TODO want widget to change color
#     points_layer.face_color = 'priority'
#     points_layer.refresh()


# @tz.curry
# def update_shown_chunk(event, viewer, chunk_map, array, alpha=1.0):
#     """
#     chunk map is a dictionary mapping chunk centers to chunk slices
#     array is the array containing the chunks
#     """
#     # TODO hack here to insert the recursive drawing
#     points = np.array(list(chunk_map.keys()))
#     distances = distance_from_camera_centre_line(points, viewer.camera)
#     depth = visual_depth(points, viewer.camera)
#     priorities = prioritised_chunk_loading(
#         depth, distances, viewer.camera.zoom, alpha=alpha
#     )
#     first_priority_idx = np.argmin(priorities)
#     first_priority_coord = tuple(points[first_priority_idx])
#     chunk_slice = chunk_map[first_priority_coord]
#     offset = [sl.start for sl in chunk_slice]
#     # TODO note that this only updates the highest resolution
#     hi_res_layer = viewer.layers['high-res']
#     hi_res_layer.data = array[chunk_slice]
#     hi_res_layer.translate = offset
#     hi_res_layer.refresh()


def get_chunk(
    chunk_slice,
    array=None,
    container=None,
    dataset=None,
    cache_manager=None,
):
    """Get a specified slice from an array (uses a cache).

    Parameters
    ----------
    chunk_slice : tuple
        a slice in array space
    array : ndarray
        one of the scales from the multiscale image
    container: str
        the zarr container name (this is used to disambiguate the cache)
    dataset: str
        the group in the zarr (this is used to disambiguate the cache)
    chunk_size: tuple
        the size of chunk that you want to fetch

    Returns
    -------
    real_array : ndarray
        an ndarray of data sliced with chunk_slice
    """
    real_array = cache_manager.get(container, dataset, chunk_slice)
    if real_array is None:
        real_array = np.asarray(array[chunk_slice].compute())
        cache_manager.put(container, dataset, chunk_slice, real_array)
    return real_array


@tz.curry
def add_subnodes_caller(
    event,
    view_slice,
    scale=0,
    viewer=None,
    cache_manager=None,
    arrays=None,
    chunk_maps=None,
    container="",
    dataset="",
    scale_factors=[],
    worker=None,
):
    """
    This function is a stub to to launch an initial recursive call of add_subnodes.
    """
    add_subnodes(
        view_slice,
        scale=scale,
        viewer=viewer,
        cache_manager=cache_manager,
        arrays=arrays,
        chunk_maps=chunk_maps,
        container=container,
        dataset=dataset,
        scale_factors=scale_factors,
        worker=worker,        
    )


def render_sequence(
    view_slice,
    scale=0,
    camera=None,
    cache_manager=None,
    arrays=None,
    chunk_maps=None,
    container="",
    dataset="",
    alpha=0.8,
    scale_factors=[],
    worker=None,
    dtype=np.uint16,
):
    layer_name = f"{container}/{dataset}/s{scale}"

    print(f"view slice {view_slice}")

    # Get some variables specific to this scale level
    min_coord = [st.start for st in view_slice]
    max_coord = [st.stop for st in view_slice]
    array = arrays[scale]
    chunk_map = chunk_maps[scale]
    scale_factor = scale_factors[scale]

    print(
        f"add_subnodes {scale} {str(view_slice)}",
        f"highres interval: {str([el.start * 2 ** scale for el in view_slice])},  {str([el.stop * 2 ** scale for el in view_slice])}",
        f"chunksize: {array.chunksize} arraysize: {array.shape}",
    )

    # Points for each chunk, for example, centers
    points = np.array(list(chunk_map.keys()))

    # Mask of whether points are within our interval, this is in array coordinates
    point_mask = np.array(
        [
            np.all(point >= min_coord) and np.all(point <= max_coord)
            for point in points
        ]
    )

    # Rescale points to world for priority calculations
    points_world = points * np.array(scale_factor)

    # Prioritize chunks using world coordinates
    distances = distance_from_camera_centre_line(points_world, camera)
    depth = visual_depth(points_world, camera)
    priorities = prioritised_chunk_loading(
        depth, distances, camera.zoom, alpha=alpha, visible=point_mask
    )

    # Find the highest priority interval for the next higher resolution
    first_priority_idx = np.argmin(priorities)

    # This node's offset in world space
    world_offset = np.array(min_coord) * np.array(scale_factor)

    # Iterate over points/chunks and add corresponding nodes when appropriate
    for idx, point in enumerate(points):
        # Render *visible* chunks, or all if we're on the last scale level
        if point_mask[idx] and (idx != first_priority_idx or scale == 0):
            coord = tuple(point)
            chunk_slice = chunk_map[coord]
            offset = [sl.start for sl in chunk_slice]
            min_interval = offset

            # find position and scale
            node_offset = (
                min_interval[0] * scale_factor[0],
                min_interval[1] * scale_factor[1],
                min_interval[2] * scale_factor[2],
            )
            LOGGER.debug(
                f"Fetching: {(scale, chunk_slice)} World offset: {node_offset}"
            )
            scale_dataset = f"{dataset}/s{scale}"
            data = get_chunk(
                chunk_slice,
                array=array,
                container=container,
                dataset=scale_dataset,
                cache_manager=cache_manager,
            )

            # TODO this will probably break with non-3D data

            # Texture slice
            texture_slice = tuple(
                [
                    slice(sl.start - offset, sl.stop - offset)
                    for sl, offset in zip(chunk_slice, min_coord)
                ]
            )

            # Texture coordinates are not necessarily world space
            # texture_offset = [
            #     chunk - layer for chunk, layer in zip(min_interval, min_coord)
            # ]
            texture_offset = [sl.start for sl in texture_slice]

            yield (
                data,
                scale,
                texture_offset,
                offset,
                world_offset,
                chunk_slice,
                texture_slice,
            )

    # recurse on top priority
    if scale > 0:
        # Get the coordinates of the first priority chunk for next scale
        first_priority_coord = tuple(points[first_priority_idx])
        chunk_slice = chunk_map[first_priority_coord]

        # Needs to be in texture space not array space
        next_scale_texture_offset = [
            sl.start - layer_offset
            for sl, layer_offset in zip(chunk_slice, min_coord)
        ]

        # Blank out the region that will be filled in by other scales
        zeros_size = [0, 0, 0]
        for d in range(len(zeros_size)):
            sl = chunk_slice[d]
            chunk_w = sl.stop - sl.start
            zeros_size[d] = chunk_w

        zdata = np.zeros(np.array(zeros_size, dtype=dtype), dtype=dtype)

        texture_slice = tuple(
            [
                slice(sl.start - offset, sl.stop - offset)
                for sl, offset in zip(chunk_slice, min_coord)
            ]
        )

        yield (
            zdata,
            scale,
            next_scale_texture_offset,
            tuple([sl.start for sl in chunk_slice]),
            world_offset,
            chunk_slice,
            texture_slice,
        )

        # Compute the relative scale factor between these layers
        relative_scale_factor = [
            this_scale / next_scale
            for this_scale, next_scale in zip(
                scale_factors[scale], scale_factors[scale - 1]
            )
        ]

        # now convert the chunk slice to the next scale
        next_chunk_slice = [
            slice(st.start * dim_scale, st.stop * dim_scale)
            for st, dim_scale in zip(chunk_slice, relative_scale_factor)
        ]

        print(
            f"Recursive add on\t{str(next_chunk_slice)} idx {first_priority_idx}",
            f"visible {point_mask[first_priority_idx]} for scale {scale} to {scale-1}\n",
            f"Relative scale factor {relative_scale_factor}",
        )
        yield from render_sequence(
            next_chunk_slice,
            scale=scale - 1,
            camera=camera,
            cache_manager=cache_manager,
            arrays=arrays,
            chunk_maps=chunk_maps,
            container=container,
            dataset=dataset,
            scale_factors=scale_factors,
            worker=worker,
            dtype=dtype,
        )


def update_chunk(
    chunk_tuple, viewer=None, container="", dataset="", scale=0, dtype=np.uint8
):

    (
        data,
        scale,
        texture_offset,
        array_offset,
        node_offset,
        chunk_slice,
        texture_slice,
    ) = chunk_tuple

    layer_name = f"{container}/{dataset}/s{scale}"
    layer = viewer.layers[layer_name]
    volume = viewer.window.qt_viewer.layer_to_visual[
        layer
    ]._layer_node.get_node(3)

    texture = volume._texture

    # Translate the layer we're rendering to the right place
    layer.translate = node_offset

    # TODO this cutoff is awful, fix the problem at the source
    #      this happened because of odd chunk sizes in the scale pyramid
    new_texture_data = np.asarray(
        data[: texture.shape[0], : texture.shape[1], : texture.shape[2]],
        dtype=dtype,
    )
    texture.set_data(new_texture_data, offset=texture_offset)
    layer.data[texture_slice] = new_texture_data

    volume.update()


def add_subnodes(
    view_slice,
    scale=0,
    viewer=None,
    cache_manager=None,
    arrays=None,
    chunk_maps=None,
    container="",
    dataset="",
    alpha=0.8,
    scale_factors=[],
    worker=None,
):
    """Recursively add multiscale chunks to a napari viewer for some multiscale arrays

    Note: scale levels are assumed to be 2x factors of each other

    Parameters
    ----------
    view_slice : tuple or list of slices
        A tuple/list of slices defining the region to display
    scale : float
        The scale level to display. 0 is highest resolution
    viewer : viewer
        a napari viewer that the nodes will be added to
    cache_manager : ChunkCacheManager
        An instance of a ChunkCacheManager for data fetching
    arrays : list
        multiscale arrays to display
    chunk_maps : list
        a list of dictionaries mapping chunk coordinates to chunk
        slices
    container : str
        the name of a zarr container, used for making unique keys in
        cache
    dataset : str
        the name of a zarr dataset, used for making unique keys in
        cache
    alpha : float
        a parameter that tunes the behavior of chunk prioritization
        see prioritised_chunk_loading for more info
    scale_factors : list of tuples
        a list of tuples of scale factors for each array
    """

    camera = viewer.camera
    dtype = np.uint16

    chunk_results = render_sequence(
        view_slice,
        scale,
        camera,
        cache_manager,
        arrays=arrays,
        chunk_maps=chunk_maps,
        container=container,
        dataset=dataset,
        scale_factors=scale_factors,
        alpha=alpha,
        dtype=dtype,
    )

    for chunk_tuple in chunk_results:
        update_chunk(
            chunk_tuple,
            viewer=viewer,
            container=container,
            dataset=dataset,
            scale=scale,
            dtype=dtype,
        )


# TODO capture some sort of metadata about scale factors
def openorganelle_mouse_kidney_labels():
    large_image = {
        "container": "s3://janelia-cosem-datasets/jrc_mus-kidney/jrc_mus-kidney.n5",
        "dataset": "labels/empanada-mito_seg",
        "scale_levels": 4,
        "scale_factors": [(1, 1, 1), (2, 2, 2), (4, 4, 4), (8, 8, 8)],
    }
    large_image["arrays"] = [
        read_xarray(
            f"{large_image['container']}/{large_image['dataset']}/s{scale}/",
            storage_options={"anon": True},
        ).data
        for scale in range(large_image["scale_levels"])
    ]
    return large_image


def openorganelle_mouse_kidney_em():
    large_image = {
        "container": "s3://janelia-cosem-datasets/jrc_mus-kidney/jrc_mus-kidney.n5",
        "dataset": "em/fibsem-uint8",
        "scale_levels": 5,
        "scale_factors": [
            (1, 1, 1),
            (2, 2, 2),
            (4, 4, 4),
            (8, 8, 8),
            (16, 16, 16),
        ],
    }
    large_image["arrays"] = [
        read_xarray(
            f"{large_image['container']}/{large_image['dataset']}/s{scale}/",
            storage_options={"anon": True},
        ).data
        for scale in range(large_image["scale_levels"])
    ]
    return large_image


# TODO this one needs testing, it is chunked over 5D
def idr0044A():
    large_image = {
        "container": "https://uk1s3.embassy.ebi.ac.uk/idr/zarr/v0.4/idr0044A/4007801.zarr",
        "dataset": "",
        "scale_levels": 5,
        "scale_factors": [
            (1, 1, 1),
            (1, 2, 2),
            (1, 4, 4),
            (1, 8, 8),
            (1, 16, 16),
        ],
    }
    large_image["arrays"] = [
        read_xarray(
            f"{large_image['container']}/{scale}/",
            #            storage_options={"anon": True},
        )
        .data[362, 0, :, :, :]
        .rechunk((512, 512, 512))
        for scale in range(large_image["scale_levels"])
    ]
    return large_image


def idr0075A():
    large_image = {
        "container": "https://uk1s3.embassy.ebi.ac.uk/idr/zarr/v0.3/idr0075A/9528933.zarr",
        "dataset": "",
        "scale_levels": 4,
        "scale_factors": [(1, 1, 1), (1, 2, 2), (1, 4, 4), (1, 8, 8)],
    }
    large_image["arrays"] = []
    for scale in range(large_image["scale_levels"]):
        url = f"{large_image['container']}/{scale}/"
        store = parse_url(url, mode="r").store

        reader = Reader(parse_url(url))
        # nodes may include images, labels etc
        nodes = list(reader())
        # first node will be the image pixel data
        image_node = nodes[0]

        large_image["arrays"].append(image_node.data[0, 0, :, :, :].squeeze())
    large_image["arrays"] = large_image["arrays"][0]
    return large_image


def luethi_zenodo_7144919():
    # Downloaded from https://zenodo.org/record/7144919#.Y-OvqhPMI0R
    large_image = {
        "container": "/Users/kharrington/Data/20200812-CardiomyocyteDifferentiation14-Cycle1.zarr",
        "dataset": "B/03/0",
        "scale_levels": 5,
        "scale_factors": [
            (1, 0.1625, 0.1625),
            (1, 0.325, 0.325),
            (1, 0.65, 0.65),
            (1, 1.3, 1.3),
            (1, 2.6, 2.6),
        ],
    }
    large_image["arrays"] = []
    for scale in range(large_image["scale_levels"]):
        result = read_xarray(
            f"{large_image['container']}/{large_image['dataset']}/{scale}/",
            #            storage_options={"anon": True},
        )

        # TODO extract scale_factors now

        # large_image["arrays"].append(result.data.rechunk((3, 10, 256, 256)))
        large_image["arrays"].append(
            result.data[2, :, :, :].rechunk((10, 256, 256)).squeeze()
        )
    return large_image


if __name__ == '__main__' and True:
    # TODO get this working with a non-remote large data sample
    # Chunked, multiscale data

    # These datasets have worked at one point in time
    large_image = openorganelle_mouse_kidney_labels()
    # large_image = idr0044A()
    # large_image = luethi_zenodo_7144919()

    # These datasets need testing

    # large_image = openorganelle_mouse_kidney_em()
    # TODO there is a problem with datasets that for some reason have shape == chunksize
    #      these datasets overflow because of memory issues

    # view_interval = ((0, 0, 0), [3 * el for el in chunk_strides[3]])
    # view_interval = ((0, 0, 0), (6144, 2048, 4096))

    cache_manager = ChunkCacheManager(cache_size=6e9)

    # TODO if the lowest scale level of these arrays still exceeds texture memory, this breaks
    multiscale_arrays = large_image["arrays"]

    # Testing with ones is pretty useful for debugging chunk placement for different scales
    # TODO notice that we're using a ones array for testing instead of real data
    # multiscale_arrays = [da.ones_like(array) for array in multiscale_arrays]

    multiscale_chunk_maps = [
        chunk_centers(array)
        for scale_level, array in enumerate(multiscale_arrays)
    ]

    multiscale_grids = [
        np.array(list(chunk_map)) for chunk_map in multiscale_chunk_maps
    ]

    # view_interval = ((0, 0, 0), multiscale_arrays[0].shape)
    view_slice = [
        slice(0, multiscale_arrays[-1].shape[idx])
        for idx in range(len(multiscale_arrays[-1].shape))
    ]
    # Forcing 3D here
    view_slice = view_slice[-3:]

    viewer = napari.Viewer(ndisplay=3)

    colormaps = {0: "red", 1: "blue", 2: "green", 3: "yellow", 4: "winter"}
    # colormaps = {0: "gray", 1: "gray", 2: "gray", 3: "gray", 4: "gray"}

    # Initialize layers
    container = large_image["container"]
    dataset = large_image["dataset"]
    scale_factors = large_image["scale_factors"]

    # Initialize worker
    worker = None

    scale = len(multiscale_arrays) - 1
    viewer.add_image(
        da.ones_like(multiscale_arrays[scale], dtype=np.uint16),
        blending="additive",
        scale=scale_factors[scale],
        colormap=colormaps[scale],
        opacity=0.8,
        rendering="mip",
        name=f"{container}/{dataset}/s{scale}",
        contrast_limits=[0, 500],
    )
    for scale in range(len(multiscale_arrays) - 1):
        relative_scale_factor = [
            this_scale / next_scale
            for this_scale, next_scale in zip(
                scale_factors[scale], scale_factors[scale - 1]
            )
        ]

        # TODO Make sure this is still smaller than the array
        scale_shape = np.array(multiscale_arrays[scale + 1].chunksize) * 2

        viewer.add_image(
            da.ones(
                scale_shape,
                dtype=np.uint16,
            ),
            blending="additive",
            scale=scale_factors[scale],
            colormap=colormaps[scale],
            opacity=0.8,
            rendering="mip",
            name=f"{container}/{dataset}/s{scale}",
            contrast_limits=[0, 500],
        )

    # Hooks and calls to start rendering
    add_subnodes(
        view_slice,
        scale=len(multiscale_arrays) - 1,
        viewer=viewer,
        cache_manager=cache_manager,
        arrays=multiscale_arrays,
        chunk_maps=multiscale_chunk_maps,
        container=large_image["container"],
        dataset=large_image["dataset"],
        scale_factors=scale_factors,
        worker=worker,
    )

    @viewer.bind_key("k")
    def refresher(event):
        add_subnodes_caller(
            event,
            view_slice,
            scale=len(multiscale_arrays) - 1,
            viewer=viewer,
            cache_manager=cache_manager,
            arrays=multiscale_arrays,
            chunk_maps=multiscale_chunk_maps,
            container=large_image["container"],
            dataset=large_image["dataset"],
            scale_factors=scale_factors,
            worker=worker,
        )

    # viewer.camera.events.connect(
    #     debounced(
    #         add_subnodes_caller(
    #             view_slice=view_slice,
    #             scale=3,
    #             viewer=viewer,
    #             cache_manager=cache_manager,
    #             arrays=multiscale_arrays,
    #             chunk_maps=multiscale_chunk_maps,
    #             container=large_image["container"],
    #             dataset=large_image["dataset"],
    #         ),
    #         timeout=100,
    #     )
    # )

    # napari.run()
