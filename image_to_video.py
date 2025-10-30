import os
from moviepy import ImageClip
from dotenv import load_dotenv
 



# video_input_dir = os.getenv("INPUT_DIR")
# video_output_dir = os.getenv("OUTPUT_DIR")


def create_video_clips_from_images(directory):
    """
    Goes through all folders in a given directory, finds all images,
    and creates a 5-second video clip of each image.
    """
    output_dir = os.path.join(directory, "output")
    os.makedirs(output_dir, exist_ok=True)

    image_extensions = [".jpg", ".jpeg", ".png", ".gif", ".webp"]

    for root, _, files in os.walk(directory):
        # Skip the output directory to avoid processing newly created videos
        if root == output_dir:
            continue

        for file in files:
            if any(file.lower().endswith(ext) for ext in image_extensions):
                image_path = os.path.join(root, file)
                video_clip_name = os.path.splitext(file)[0] + ".mp4"
                video_clip_path = os.path.join(output_dir, video_clip_name)

                # Check if the video clip already exists before processing
                if os.path.exists(video_clip_path):
                    print(f"Video clip already exists for {file}. Skipping.")
                    continue  # Skip to the next file


                try:
                    # Create a 5-second video clip from the image
                    clip = ImageClip(image_path, duration=5)

                    # Check if the clip is valid
                    if clip.size == (0, 0):
                        print(f"Invalid image file {image_path}. Skipping.")
                        continue

                    # Set a solid color background (e.g., black) for images with transparency
                    if clip.mask is not None:
                        clip = clip.on_color(
                            color=(0, 0, 0),
                            col_opacity=1
                        )

                    # Write the video file to disk
                    clip.write_videofile(video_clip_path, fps=24, codec='libx264')
                    print(f"Successfully created video clip: {video_clip_path}")

                except Exception as e:
                    print(f"Could not process image {image_path}. Error: {e}")

if __name__ == "__main__":
    create_video_clips_from_images("/input")
    # target_directory = input("Enter the directory path containing the images: ")
    # if os.path.isdir(target_directory):
    #     create_video_clips_from_images(target_directory)
    # else:
    #     print("Error: The provided path is not a valid directory.")
