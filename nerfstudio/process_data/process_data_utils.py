# Copyright 2022 the Regents of the University of California, Nerfstudio Team and contributors. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Helper utils for processing data into the nerfstudio format."""

import math
import random
import re
import shutil
import sys
from enum import Enum
from pathlib import Path
from typing import List, Literal, Optional, OrderedDict, Tuple, Union, cast

import cv2
import imageio

try:
    import rawpy
except ImportError:
    import newrawpy as rawpy  # type: ignore

import numpy as np

from nerfstudio.utils.rich_utils import CONSOLE, status
from nerfstudio.utils.scripts import run_command

POLYCAM_UPSCALING_TIMES = 2

"""Lowercase suffixes to treat as raw image."""
ALLOWED_RAW_EXTS = [".cr2"]
"""Suffix to use for converted images from raw."""
RAW_CONVERTED_SUFFIX = ".jpg"


class CameraModel(Enum):
    """Enum for camera types."""

    OPENCV = "OPENCV"
    OPENCV_FISHEYE = "OPENCV_FISHEYE"
    EQUIRECTANGULAR = "EQUIRECTANGULAR"
    PINHOLE = "PINHOLE"
    SIMPLE_PINHOLE = "SIMPLE_PINHOLE"


CAMERA_MODELS = {
    "perspective": CameraModel.OPENCV,
    "fisheye": CameraModel.OPENCV_FISHEYE,
    "equirectangular": CameraModel.EQUIRECTANGULAR,
    "pinhole": CameraModel.PINHOLE,
    "simple_pinhole": CameraModel.SIMPLE_PINHOLE,
}


def list_images(data: Path, recursive: bool = True) -> List[Path]:
    """Lists all supported images in a directory

    Args:
        data: Path to the directory of images.
        recursive: Whether to search check nested folders in `data`.
    Returns:
        Paths to images contained in the directory
    """
    allowed_exts = [".jpg", ".jpeg", ".png", ".tif", ".tiff"] + ALLOWED_RAW_EXTS
    glob_str = "**/[!.]*" if recursive else "[!.]*"
    image_paths = sorted([p for p in data.glob(glob_str) if p.suffix.lower() in allowed_exts])
    return image_paths


def get_image_filenames(directory: Path, max_num_images: int = -1) -> Tuple[List[Path], int]:
    """Returns a list of image filenames in a directory.

    Args:
        dir: Path to the directory.
        max_num_images: The maximum number of images to return. -1 means no limit.
    Returns:
        A tuple of A list of image filenames, number of original image paths.
    """
    image_paths = list_images(directory)
    num_orig_images = len(image_paths)

    if max_num_images != -1 and num_orig_images > max_num_images:
        idx = np.round(np.linspace(0, num_orig_images - 1, max_num_images)).astype(int)
    else:
        idx = np.arange(num_orig_images)

    image_filenames = cast(List[Path], list(np.array(image_paths)[idx]))

    return image_filenames, num_orig_images


def get_num_frames_in_video(video: Path) -> int:
    """Returns the number of frames in a video.

    Args:
        video: Path to a video.

    Returns:
        The number of frames in a video.
    """
    cmd = f'ffprobe -v error -select_streams v:0 -count_packets \
            -show_entries stream=nb_read_packets -of csv=p=0 "{video}"'
    output = run_command(cmd)
    assert output is not None
    number_match = re.search(r"\d+", output)
    assert number_match is not None
    return int(number_match[0])


def convert_video_to_images(
    video_path: Path,
    image_dir: Path,
    num_frames_target: int,
    num_downscales: int,
    crop_factor: Tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0),
    verbose: bool = False,
    image_prefix: str = "frame_",
    keep_image_dir: bool = False,
    random_seed: Optional[int] = None,
) -> Tuple[List[str], int]:
    """Converts a video into a sequence of images.

    Args:
        video_path: Path to the video.
        output_dir: Path to the output directory.
        num_frames_target: Number of frames to extract.
        num_downscales: Number of times to downscale the images. Downscales by 2 each time.
        crop_factor: Portion of the image to crop. Should be in [0,1] (top, bottom, left, right)
        verbose: If True, logs the output of the command.
        image_prefix: Prefix to use for the image filenames.
        keep_image_dir: If True, don't delete the output directory if it already exists.
        random_seed: If set, the seed used to choose the frames of the video
    Returns:
        A tuple containing summary of the conversion and the number of extracted frames.
    """

    # If keep_image_dir is False, then remove the output image directory and its downscaled versions
    if not keep_image_dir:
        for i in range(num_downscales + 1):
            dir_to_remove = image_dir if i == 0 else f"{image_dir}_{2**i}"
            shutil.rmtree(dir_to_remove, ignore_errors=True)
    image_dir.mkdir(exist_ok=True, parents=True)

    for i in crop_factor:
        if i < 0 or i > 1:
            CONSOLE.print("[bold red]Error: Invalid crop factor. All crops must be in [0,1].")
            sys.exit(1)

    if video_path.is_dir():
        CONSOLE.print(f"[bold red]Error: Video path is a directory, not a path: {video_path}")
        sys.exit(1)
    if video_path.exists() is False:
        CONSOLE.print(f"[bold red]Error: Video does not exist: {video_path}")
        sys.exit(1)

    with status(msg="Converting video to images...", spinner="bouncingBall", verbose=verbose):
        num_frames = get_num_frames_in_video(video_path)
        if num_frames == 0:
            CONSOLE.print(f"[bold red]Error: Video has no frames: {video_path}")
            sys.exit(1)
        CONSOLE.print("Number of frames in video:", num_frames)

        ffmpeg_cmd = f'ffmpeg -i "{video_path}"'

        crop_cmd = ""
        if crop_factor != (0.0, 0.0, 0.0, 0.0):
            height = 1 - crop_factor[0] - crop_factor[1]
            width = 1 - crop_factor[2] - crop_factor[3]
            start_x = crop_factor[2]
            start_y = crop_factor[0]
            crop_cmd = f"crop=w=iw*{width}:h=ih*{height}:x=iw*{start_x}:y=ih*{start_y},"

        downscale_chains = [f"[t{i}]scale=iw/{2**i}:ih/{2**i}[out{i}]" for i in range(num_downscales + 1)]
        downscale_dirs = [Path(str(image_dir) + (f"_{2**i}" if i > 0 else "")) for i in range(num_downscales + 1)]
        downscale_paths = [downscale_dirs[i] / f"{image_prefix}%05d.png" for i in range(num_downscales + 1)]

        for dir in downscale_dirs:
            dir.mkdir(parents=True, exist_ok=True)

        downscale_chain = (
            f"split={num_downscales + 1}"
            + "".join([f"[t{i}]" for i in range(num_downscales + 1)])
            + ";"
            + ";".join(downscale_chains)
        )

        ffmpeg_cmd += " -vsync vfr"

        # Evenly distribute frame selection if random seed does not exist
        spacing = num_frames // num_frames_target
        if random_seed:
            random.seed(random_seed)
            frame_indices = sorted(random.sample(range(num_frames), num_frames_target))
            select_cmd = "select='" + "+".join([f"eq(n\,{idx})" for idx in frame_indices]) + "',setpts=N/TB,"
            CONSOLE.print(f"Extracting {num_frames_target} frames using seed {random_seed} random selection.")
        elif spacing > 1:
            CONSOLE.print(f"Extracting {math.ceil(num_frames / spacing)} frames in evenly spaced intervals")
            select_cmd = f"thumbnail={spacing},setpts=N/TB,"
        else:
            CONSOLE.print("[bold red]Can't satisfy requested number of frames. Extracting all frames.")
            ffmpeg_cmd += " -pix_fmt bgr8"
            select_cmd = ""

        downscale_cmd = f' -filter_complex "{select_cmd}{crop_cmd}{downscale_chain}"' + "".join(
            [f' -map "[out{i}]" "{downscale_paths[i]}"' for i in range(num_downscales + 1)]
        )

        ffmpeg_cmd += downscale_cmd

        run_command(ffmpeg_cmd, verbose=verbose)

        num_final_frames = len(list(image_dir.glob("*.png")))
        summary_log = []
        summary_log.append(f"Starting with {num_frames} video frames")
        summary_log.append(f"We extracted {num_final_frames} images with prefix '{image_prefix}'")
        CONSOLE.log("[bold green]:tada: Done converting video to images.")

        return summary_log, num_final_frames


def copy_images_list(
    image_paths: List[Path],
    image_dir: Path,
    num_downscales: int,
    image_prefix: str = "frame_",
    crop_border_pixels: Optional[int] = None,
    crop_factor: Tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0),
    verbose: bool = False,
    keep_image_dir: bool = False,
    upscale_factor: Optional[int] = None,
    nearest_neighbor: bool = False,
    same_dimensions: bool = True,
) -> List[Path]:
    """Copy all images in a list of Paths. Useful for filtering from a directory.
    Args:
        image_paths: List of Paths of images to copy to a new directory.
        image_dir: Path to the output directory.
        num_downscales: Number of times to downscale the images. Downscales by 2 each time.
        image_prefix: Prefix for the image filenames.
        crop_border_pixels: If not None, crops each edge by the specified number of pixels.
        crop_factor: Portion of the image to crop. Should be in [0,1] (top, bottom, left, right)
        verbose: If True, print extra logging.
        keep_image_dir: If True, don't delete the output directory if it already exists.
    Returns:
        A list of the copied image Paths.
    """

    # Remove original directory and its downscaled versions
    # only if we provide a proper image folder path and keep_image_dir is False
    if image_dir.is_dir() and len(image_paths) and not keep_image_dir:
        # check that output directory is not the same as input directory
        if image_dir != image_paths[0].parent:
            for i in range(num_downscales + 1):
                dir_to_remove = image_dir if i == 0 else f"{image_dir}_{2**i}"
                shutil.rmtree(dir_to_remove, ignore_errors=True)
    image_dir.mkdir(exist_ok=True, parents=True)

    copied_image_paths = []

    # Images should be 1-indexed for the rest of the pipeline.
    for idx, image_path in enumerate(image_paths):
        if verbose:
            CONSOLE.log(f"Copying image {idx + 1} of {len(image_paths)}...")
        copied_image_path = image_dir / f"{image_prefix}{idx + 1:05d}{image_path.suffix}"
        try:
            # if CR2 raw, we want to read raw and write RAW_CONVERTED_SUFFIX, and change the file suffix for downstream processing
            if image_path.suffix.lower() in ALLOWED_RAW_EXTS:
                copied_image_path = image_dir / f"{image_prefix}{idx + 1:05d}{RAW_CONVERTED_SUFFIX}"
                with rawpy.imread(str(image_path)) as raw:
                    rgb = raw.postprocess()
                imageio.imsave(copied_image_path, rgb)
                image_paths[idx] = copied_image_path
            elif same_dimensions:
                # Fast path; just copy the file
                shutil.copy(image_path, copied_image_path)
            else:
                # Slow path; let ffmpeg perform autorotation (and clear metadata)
                ffmpeg_cmd = f"ffmpeg -y -i {image_path} -metadata:s:v:0 rotate=0 {copied_image_path}"
                if verbose:
                    CONSOLE.log(f"... {ffmpeg_cmd}")
                run_command(ffmpeg_cmd, verbose=verbose)
        except shutil.SameFileError:
            pass
        copied_image_paths.append(copied_image_path)

    nn_flag = "" if not nearest_neighbor else ":flags=neighbor"
    downscale_chains = [f"[t{i}]scale=iw/{2**i}:ih/{2**i}{nn_flag}[out{i}]" for i in range(num_downscales + 1)]
    downscale_dirs = [Path(str(image_dir) + (f"_{2**i}" if i > 0 else "")) for i in range(num_downscales + 1)]

    for dir in downscale_dirs:
        dir.mkdir(parents=True, exist_ok=True)

    downscale_chain = (
        f"split={num_downscales + 1}"
        + "".join([f"[t{i}]" for i in range(num_downscales + 1)])
        + ";"
        + ";".join(downscale_chains)
    )

    num_frames = len(image_paths)
    # ffmpeg batch commands assume all images are the same dimensions.
    # When this is not the case (e.g. mixed portrait and landscape images), we need to do individually.
    # (Unfortunately, that is much slower.)
    for framenum in range(1, (1 if same_dimensions else num_frames) + 1):
        framename = f"{image_prefix}%05d" if same_dimensions else f"{image_prefix}{framenum:05d}"
        ffmpeg_cmd = f'ffmpeg -y -noautorotate -i "{image_dir / f"{framename}{copied_image_paths[0].suffix}"}" '

        crop_cmd = ""
        if crop_border_pixels is not None:
            crop_cmd = f"crop=iw-{crop_border_pixels * 2}:ih-{crop_border_pixels * 2}[cropped];[cropped]"
        elif crop_factor != (0.0, 0.0, 0.0, 0.0):
            height = 1 - crop_factor[0] - crop_factor[1]
            width = 1 - crop_factor[2] - crop_factor[3]
            start_x = crop_factor[2]
            start_y = crop_factor[0]
            crop_cmd = f"crop=w=iw*{width}:h=ih*{height}:x=iw*{start_x}:y=ih*{start_y}[cropped];[cropped]"

        select_cmd = "[0:v]"
        if upscale_factor is not None:
            select_cmd = f"[0:v]scale=iw*{upscale_factor}:ih*{upscale_factor}:flags=neighbor[upscaled];[upscaled]"

        downscale_cmd = f' -filter_complex "{select_cmd}{crop_cmd}{downscale_chain}"' + "".join(
            [
                f' -map "[out{i}]" -q:v 2 "{downscale_dirs[i] / f"{framename}{copied_image_paths[0].suffix}"}"'
                for i in range(num_downscales + 1)
            ]
        )

        ffmpeg_cmd += downscale_cmd
        if verbose:
            CONSOLE.log(f"... {ffmpeg_cmd}")
        run_command(ffmpeg_cmd, verbose=verbose)

    if num_frames == 0:
        CONSOLE.log("[bold red]:skull: No usable images in the data folder.")
    else:
        CONSOLE.log(f"[bold green]:tada: Done copying images with prefix '{image_prefix}'.")

    return copied_image_paths


def copy_and_upscale_polycam_depth_maps_list(
    polycam_depth_image_filenames: List[Path],
    depth_dir: Path,
    num_downscales: int,
    crop_border_pixels: Optional[int] = None,
    verbose: bool = False,
) -> List[Path]:
    """
    Copy depth maps to working location and upscale them to match the RGB images dimensions and finally crop them
    equally as RGB Images.
    Args:
        polycam_depth_image_filenames: List of Paths of images to copy to a new directory.
        depth_dir: Path to the output directory.
        crop_border_pixels: If not None, crops each edge by the specified number of pixels.
        verbose: If True, print extra logging.
    Returns:
        A list of the copied depth maps paths.
    """
    depth_dir.mkdir(parents=True, exist_ok=True)

    # copy and upscale them to new directory
    with status(
        msg="[bold yellow] Upscaling depth maps...",
        spinner="growVertical",
        verbose=verbose,
    ):
        upscale_factor = 2**POLYCAM_UPSCALING_TIMES
        assert upscale_factor > 1
        assert isinstance(upscale_factor, int)

        copied_depth_map_paths = copy_images_list(
            image_paths=polycam_depth_image_filenames,
            image_dir=depth_dir,
            num_downscales=num_downscales,
            crop_border_pixels=crop_border_pixels,
            verbose=verbose,
            upscale_factor=upscale_factor,
            nearest_neighbor=True,
        )

    CONSOLE.log("[bold green]:tada: Done upscaling depth maps.")
    return copied_depth_map_paths


def copy_images(
    data: Path,
    image_dir: Path,
    image_prefix: str = "frame_",
    verbose: bool = False,
    keep_image_dir: bool = False,
    crop_factor: Tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0),
    num_downscales: int = 0,
    same_dimensions: bool = True,
) -> OrderedDict[Path, Path]:
    """Copy images from a directory to a new directory.

    Args:
        data: Path to the directory of images.
        image_dir: Path to the output directory.
        image_prefix: Prefix for the image filenames.
        verbose: If True, print extra logging.
        crop_factor: Portion of the image to crop. Should be in [0,1] (top, bottom, left, right)
        keep_image_dir: If True, don't delete the output directory if it already exists.
    Returns:
        The mapping from the original filenames to the new ones.
    """
    with status(msg="[bold yellow]Copying images...", spinner="bouncingBall", verbose=verbose):
        image_paths = list_images(data)

        if len(image_paths) == 0:
            CONSOLE.log("[bold red]:skull: No usable images in the data folder.")
            sys.exit(1)

        copied_images = copy_images_list(
            image_paths=image_paths,
            image_dir=image_dir,
            crop_factor=crop_factor,
            verbose=verbose,
            image_prefix=image_prefix,
            keep_image_dir=keep_image_dir,
            num_downscales=num_downscales,
            same_dimensions=same_dimensions,
        )
        return OrderedDict((original_path, new_path) for original_path, new_path in zip(image_paths, copied_images))


def downscale_images(
    image_dir: Path,
    num_downscales: int,
    folder_name: str = "images",
    nearest_neighbor: bool = False,
    verbose: bool = False,
) -> str:
    """(Now deprecated; much faster integrated into copy_images.)
    Downscales the images in the directory. Uses FFMPEG.

    Args:
        image_dir: Path to the directory containing the images.
        num_downscales: Number of times to downscale the images. Downscales by 2 each time.
        folder_name: Name of the output folder
        nearest_neighbor: Use nearest neighbor sampling (useful for depth images)
        verbose: If True, logs the output of the command.

    Returns:
        Summary of downscaling.
    """

    if num_downscales == 0:
        return "No downscaling performed."

    with status(
        msg="[bold yellow]Downscaling images...",
        spinner="growVertical",
        verbose=verbose,
    ):
        downscale_factors = [2**i for i in range(num_downscales + 1)[1:]]
        for downscale_factor in downscale_factors:
            assert downscale_factor > 1
            assert isinstance(downscale_factor, int)
            downscale_dir = image_dir.parent / f"{folder_name}_{downscale_factor}"
            downscale_dir.mkdir(parents=True, exist_ok=True)
            # Using %05d ffmpeg commands appears to be unreliable (skips images).
            for f in list_images(image_dir):
                filename = f.name
                nn_flag = "" if not nearest_neighbor else ":flags=neighbor"
                ffmpeg_cmd = [
                    f'ffmpeg -y -noautorotate -i "{image_dir / filename}" ',
                    f"-q:v 2 -vf scale=iw/{downscale_factor}:ih/{downscale_factor}{nn_flag} ",
                    f'"{downscale_dir / filename}"',
                ]
                ffmpeg_cmd = " ".join(ffmpeg_cmd)
                run_command(ffmpeg_cmd, verbose=verbose)

    CONSOLE.log("[bold green]:tada: Done downscaling images.")
    downscale_text = [f"[bold blue]{2 ** (i + 1)}x[/bold blue]" for i in range(num_downscales)]
    downscale_text = ", ".join(downscale_text[:-1]) + " and " + downscale_text[-1]
    return f"We downsampled the images by {downscale_text}"


def find_tool_feature_matcher_combination(
    sfm_tool: Literal["any", "colmap", "hloc"],
    feature_type: Literal[
        "any",
        "sift",
        "superpoint",
        "superpoint_aachen",
        "superpoint_max",
        "superpoint_inloc",
        "r2d2",
        "d2net-ss",
        "sosnet",
        "disk",
    ],
    matcher_type: Literal[
        "any",
        "NN",
        "superglue",
        "superglue-fast",
        "NN-superpoint",
        "NN-ratio",
        "NN-mutual",
        "adalam",
        "disk+lightglue",
        "superpoint+lightglue",
    ],
) -> Union[
    Tuple[None, None, None],
    Tuple[
        Literal["colmap", "hloc"],
        Literal[
            "sift",
            "superpoint_aachen",
            "superpoint_max",
            "superpoint_inloc",
            "r2d2",
            "d2net-ss",
            "sosnet",
            "disk",
        ],
        Literal[
            "NN",
            "superglue",
            "superglue-fast",
            "NN-superpoint",
            "NN-ratio",
            "NN-mutual",
            "adalam",
            "disk+lightglue",
            "superpoint+lightglue",
        ],
    ],
]:
    """Find a valid combination of sfm tool, feature type, and matcher type.
    Basically, replace the default parameters 'any' by usable value

    Args:
        sfm_tool: Sfm tool name (any, colmap, hloc)
        feature_type: Type of image features (any, sift, superpoint, ...)
        matcher_type: Type of matching algorithm (any, NN, superglue,...)

    Returns:
        Tuple of sfm tool, feature type, and matcher type.
        Returns (None,None,None) if no valid combination can be found
    """
    if sfm_tool == "any":
        if (feature_type in ("any", "sift")) and (matcher_type in ("any", "NN")):
            sfm_tool = "colmap"
        else:
            sfm_tool = "hloc"

    if sfm_tool == "colmap":
        if (feature_type not in ("any", "sift")) or (matcher_type not in ("any", "NN")):
            return (None, None, None)
        return ("colmap", "sift", "NN")
    if sfm_tool == "hloc":
        if feature_type in ("any", "superpoint"):
            feature_type = "superpoint_aachen"

        if matcher_type == "any":
            matcher_type = "superpoint+lightglue"
        elif matcher_type == "NN":
            matcher_type = "NN-mutual"

        return (sfm_tool, feature_type, matcher_type)
    return (None, None, None)


def generate_circle_mask(height: int, width: int, percent_radius) -> Optional[np.ndarray]:
    """generate a circle mask of the given size.

    Args:
        height: The height of the mask.
        width: The width of the mask.
        percent_radius: The radius of the circle as a percentage of the image diagonal size.

    Returns:
        The mask or None if the radius is too large.
    """
    if percent_radius <= 0.0:
        CONSOLE.log("[bold red]:skull: The radius of the circle mask must be positive.")
        sys.exit(1)
    if percent_radius >= 1.0:
        return None
    mask = np.zeros((height, width), dtype=np.uint8)
    center = (width // 2, height // 2)
    radius = int(percent_radius * np.sqrt(width**2 + height**2) / 2.0)
    cv2.circle(mask, center, radius, 1, -1)  # type: ignore
    return mask


def generate_crop_mask(height: int, width: int, crop_factor: Tuple[float, float, float, float]) -> Optional[np.ndarray]:
    """generate a crop mask of the given size.

    Args:
        height: The height of the mask.
        width: The width of the mask.
        crop_factor: The percent of the image to crop in each direction [top, bottom, left, right].

    Returns:
        The mask or None if no cropping is performed.
    """
    if np.all(np.array(crop_factor) == 0.0):
        return None
    if np.any(np.array(crop_factor) < 0.0) or np.any(np.array(crop_factor) > 1.0):
        CONSOLE.log("[bold red]Invalid crop percentage, must be between 0 and 1.")
        sys.exit(1)
    top, bottom, left, right = crop_factor
    mask = np.zeros((height, width), dtype=np.uint8)
    top = int(top * height)
    bottom = int(bottom * height)
    left = int(left * width)
    right = int(right * width)
    mask[top : height - bottom, left : width - right] = 1.0
    return mask


def generate_mask(
    height: int,
    width: int,
    crop_factor: Tuple[float, float, float, float],
    percent_radius: float,
) -> Optional[np.ndarray]:
    """generate a mask of the given size.

    Args:
        height: The height of the mask.
        width: The width of the mask.
        crop_factor: The percent of the image to crop in each direction [top, bottom, left, right].
        percent_radius: The radius of the circle as a percentage of the image diagonal size.

    Returns:
        The mask or None if no mask is needed.
    """
    crop_mask = generate_crop_mask(height, width, crop_factor)
    circle_mask = generate_circle_mask(height, width, percent_radius)
    if crop_mask is None:
        return circle_mask
    if circle_mask is None:
        return crop_mask
    return crop_mask * circle_mask


def save_mask(
    image_dir: Path,
    num_downscales: int,
    crop_factor: Tuple[float, float, float, float] = (0, 0, 0, 0),
    percent_radius: float = 1.0,
) -> Optional[Path]:
    """Save a mask for each image in the image directory.

    Args:
        image_dir: The directory containing the images.
        num_downscales: The number of downscaling levels.
        crop_factor: The percent of the image to crop in each direction [top, bottom, left, right].
        percent_radius: The radius of the circle as a percentage of the image diagonal size.

    Returns:
        The path to the mask file or None if no mask is needed.
    """
    image_path = next(image_dir.glob("frame_*"))
    image = cv2.imread(str(image_path))
    height, width = image.shape[:2]
    mask = generate_mask(height, width, crop_factor, percent_radius)
    if mask is None:
        return None
    mask *= 255
    mask_path = image_dir.parent / "masks"
    mask_path.mkdir(exist_ok=True)
    cv2.imwrite(str(mask_path / "mask.png"), mask)
    downscale_factors = [2**i for i in range(num_downscales + 1)[1:]]
    for downscale in downscale_factors:
        mask_path_i = image_dir.parent / f"masks_{downscale}"
        mask_path_i.mkdir(exist_ok=True)
        mask_path_i = mask_path_i / "mask.png"
        mask_i = cv2.resize(
            mask,
            (width // downscale, height // downscale),
            interpolation=cv2.INTER_NEAREST,
        )
        cv2.imwrite(str(mask_path_i), mask_i)
    CONSOLE.log(":tada: Generated and saved masks.")
    return mask_path / "mask.png"
