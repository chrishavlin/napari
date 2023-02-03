import itertools
import dask.array as da
from skimage import data
import numpy as np
import pandas as pd
import napari
import toolz as tz
from psygnal import debounced
from cachey import Cache

from scipy.spatial.transform import Rotation as R

# from https://github.com/janelia-cosem/fibsem-tools
#   pip install fibsem-tools
from fibsem_tools.io import read_xarray

colormaps = {0: "red", 1: "blue", 2: "green", 3: "yellow"}


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
        slice_key = ",".join(
            [f"{st.start}:{st.stop}:{st.step}" for st in chunk_slice]
        )
        return f"{container}/{dataset}@({slice_key})"

    def get(self, container, dataset, chunk_slice):
        return self.c.get(
            self.get_container_key(container, dataset, chunk_slice)
        )


def chunk_centers(array: da.Array, scale=1.0):
    """Make a dictionary mapping chunk centers to chunk slices.

    Parameters
    ----------
    array: dask Array
        The input array.
    scale_factor: float
        The scale multiplier for center coordinates.
        TODO: This should become an array/tuple scale is dimension specific.

    Returns
    -------
    chunk_map : dict {tuple of float: tuple of slices}
        A dictionary mapping chunk centers to chunk slices.
    """

    # Rescale the chunks
    chunks = [[val * scale for val in chunks] for chunks in array.chunks]

    start_pos = [np.cumsum(sizes) - sizes for sizes in chunks]
    middle_pos = [np.cumsum(sizes) - (np.array(sizes) / 2) for sizes in chunks]
    end_pos = [np.cumsum(sizes) for sizes in chunks]
    all_start_pos = list(itertools.product(*start_pos))
    all_middle_pos = list(itertools.product(*middle_pos))
    all_end_pos = list(itertools.product(*end_pos))
    chunk_slices = []
    for start, end in zip(all_start_pos, all_end_pos):
        chunk_slice = [
            slice(start_i, end_i) for start_i, end_i in zip(start, end)
        ]
        chunk_slices.append(tuple(chunk_slice))

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
        chunk_load_priority[not visible] = np.inf
    return chunk_load_priority


@tz.curry
def update_point_colors(event, viewer, alpha=1.0):
    """Update the points based on their distance to current camera.

    Parameters:
    -----------
    viewer : napari.Viewer
        Current viewer
    event : camera.events.angles event
        The event triggered by changing the camera angles
    """
    # TODO we need a grid for each scale, or the grid needs to include all scales
    points_layer = viewer.layers['grid']
    points = points_layer.data
    distances = distance_from_camera_centre_line(points, viewer.camera)
    depth = visual_depth(points, viewer.camera)
    priorities = prioritised_chunk_loading(
        depth, distances, viewer.camera.zoom, alpha=alpha
    )
    points_layer.features = pd.DataFrame(
        {'distance': distances, 'depth': depth, 'priority': priorities}
    )
    # TODO want widget to change color
    points_layer.face_color = 'priority'
    points_layer.refresh()


@tz.curry
def update_shown_chunk(event, viewer, chunk_map, array, alpha=1.0):
    """
    chunk map is a dictionary mapping chunk centers to chunk slices
    array is the array containing the chunks
    """
    # TODO hack here to insert the recursive drawing
    points = np.array(list(chunk_map.keys()))
    distances = distance_from_camera_centre_line(points, viewer.camera)
    depth = visual_depth(points, viewer.camera)
    priorities = prioritised_chunk_loading(
        depth, distances, viewer.camera.zoom, alpha=alpha
    )
    first_priority_idx = np.argmin(priorities)
    first_priority_coord = tuple(points[first_priority_idx])
    chunk_slice = chunk_map[first_priority_coord]
    offset = [sl.start for sl in chunk_slice]
    # TODO note that this only updates the highest resolution
    hi_res_layer = viewer.layers['high-res']
    hi_res_layer.data = array[chunk_slice]
    hi_res_layer.translate = offset
    hi_res_layer.refresh()


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


# TODO i know this shouldn't be here, but keybindings are annoying

@tz.curry
def add_subnodes_caller(
    view_slice,
    scale=0,
    viewer=None,
    cache_manager=None,
    arrays=None,
    chunk_maps=None,
    container="",
    dataset="",
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
    )


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
):
    """Recursively add multiscale chunks to a napari viewer for some multiscale arrays

    Note: scale levels are assumed to be 2x factors of each other

    TODO maybe we should smoosh chunks together within the same resolution level

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
    """

    # Delete old nodes because we will replace them
    # TODO consider doing this closer to node adding time to minimize blank screen time
    layers_to_delete = [
        layer for layer in viewer.layers if f"chunk_scale{scale}_" in layer.name
    ]
    # Remove layers
    for layer in layers_to_delete:
        viewer.layers.remove(layer)

    chunk_strides = [
        [2**scale * el for el in arrays[scale].chunksize]
        for scale in range(len(arrays))
    ]

    min_coord = [st.start for st in view_slice]
    max_coord = [st.stop for st in view_slice]
    array = arrays[scale]
    chunk_map = chunk_maps[scale]

    print(
        f"add_subnodes {scale} {view_slice} \nhighres interval: {[el.start * 2 ** scale for el in view_slice]},  {[el.stop * 2 ** scale for el in view_slice]} chunksize: {array.chunksize} arraysize: {array.shape}"
    )

    # Points for each chunk, for example, centers
    points = np.array(list(chunk_map.keys()))

    # Mask of whether points are within our interval, this is in array coordinates
    point_mask = [
        np.all(point >= min_coord) and np.all(point <= max_coord)
        for point in points
    ]

    # Rescale points to world for priority calculations
    points_world = points * 2**scale

    # Prioritize chunks using world coordinates
    distances = distance_from_camera_centre_line(points_world, viewer.camera)
    depth = visual_depth(points_world, viewer.camera)
    priorities = prioritised_chunk_loading(
        depth, distances, viewer.camera.zoom, alpha=alpha, visible=point_mask
    )

    # Find the highest priority interval for the next higher resolution
    first_priority_idx = np.argmin(priorities)

    # Iterate over points/chunks and add corresponding nodes when appropriate
    for idx, point in enumerate(points):
        # Render *visible* chunks, or all if we're on the last scale level
        if point_mask[idx] and (idx != first_priority_idx or scale == 0):
            coord = tuple(point)
            chunk_slice = chunk_map[coord]
            offset = [sl.start for sl in chunk_slice]
            endpoint = [sl.stop for sl in chunk_slice]
            min_interval = offset

            # find position and scale
            node_offset = (
                min_interval[2] * 2**scale,
                min_interval[1] * 2**scale,
                min_interval[0] * scale**2,
            )
            print(
                f"Fetching: {(scale, chunk_slice)} World offset: {node_offset}"
            )
            scale_dataset = f"{dataset}/s{scale}"
            data = get_chunk(
                chunk_slice,
                array=array,
                container=container,
                dataset=scale_dataset,
                cache_manager=cache_manager,
            ).transpose()
            node_scale = (
                2**scale,
                2**scale,
                2**scale,
            )
            viewer.add_image(
                data,
                scale=node_scale,
                translate=node_offset,
                name=f"chunk_scale{scale}_{min_interval[0]}_{min_interval[1]}_{min_interval[2]}",
                blending="additive",
                colormap=colormaps[scale],
                opacity=0.8,
                rendering="mip",
            )
            # set data

    # recurse on top priority
    if scale > 0:
        # Get the coordinates of the first priority chunk for next scale
        first_priority_coord = tuple(points[first_priority_idx])
        chunk_slice = chunk_map[first_priority_coord]
        # now convert the chunk slice to the next scale
        next_chunk_slice = [
            slice(st.start * 2, st.stop * 2) for st in chunk_slice
        ]

        # TODO check what is happening with the intervals. currently intervals are not recursively contained

        print(f"\nSource interval\t{min_coord}, {max_coord}")
        print(
            f"Recursive add on\t{next_chunk_slice} idx {first_priority_idx} visible {point_mask[first_priority_idx]} for scale {scale} to {scale-1}\n"
        )
        add_subnodes(
            next_chunk_slice,
            scale=scale - 1,
            viewer=viewer,
            cache_manager=cache_manager,
            arrays=arrays,
            chunk_maps=chunk_maps,
            container=container,
            dataset=dataset,
        )


# if __name__ == '__main__':
if True:
    # TODO get this working with a non-remote large data sample
    # Chunked, multiscale data
    large_image = {
        "container": "s3://janelia-cosem-datasets/jrc_mus-kidney/jrc_mus-kidney.n5",
        "dataset": "labels/empanada-mito_seg",
        "scale_levels": 4,
    }
    large_image["arrays"] = [
        read_xarray(
            f"{large_image['container']}/{large_image['dataset']}/s{scale}/",
            storage_options={"anon": True},
        )
        for scale in range(large_image["scale_levels"])
    ]

    # view_interval = ((0, 0, 0), [3 * el for el in chunk_strides[3]])
    # view_interval = ((0, 0, 0), (6144, 2048, 4096))

    cache_manager = ChunkCacheManager()

    # Make our xarray data look more like typical napari multiscale data
    multiscale_arrays = [array.data for array in large_image["arrays"]]

    # Testing with ones is pretty useful for debugging chunk placement for different scales
    multiscale_arrays = [da.ones_like(array) for array in multiscale_arrays]

    multiscale_chunk_maps = [
        chunk_centers(array)
        for scale_level, array in enumerate(multiscale_arrays)
    ]

    multiscale_grids = [
        np.array(list(chunk_map)) for chunk_map in multiscale_chunk_maps
    ]

    # view_interval = ((0, 0, 0), multiscale_arrays[0].shape)
    view_slice = [
        slice(0, multiscale_arrays[3].shape[idx])
        for idx in range(len(multiscale_arrays[3].shape))
    ]

    viewer = napari.Viewer(ndisplay=3)

    add_subnodes_caller(
        view_slice,
        scale=3,
        viewer=viewer,
        cache_manager=cache_manager,
        arrays=multiscale_arrays,
        chunk_maps=multiscale_chunk_maps,
        container=large_image["container"],
        dataset=large_image["dataset"],
    )

    @viewer.bind_key("k")
    def refresher(event):
        add_subnodes_caller(
            view_slice,
            scale=3,
            viewer=viewer,
            cache_manager=cache_manager,
            arrays=multiscale_arrays,
            chunk_maps=multiscale_chunk_maps,
            container=large_image["container"],
            dataset=large_image["dataset"],
        )        
    
    # viewer.camera.events.connect(
    #     debounced(
    #         add_subnodes_caller(
    #             view_slice,
    #             scale=3,
    #             viewer=viewer,
    #             cache_manager=cache_manager,
    #             arrays=multiscale_arrays,
    #             chunk_maps=multiscale_chunk_maps,
    #             container=large_image["container"],
    #             dataset=large_image["dataset"],
    #         ),
    #         timeout=1000,
    #     )
    # )

    # napari.run()
