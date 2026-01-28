import os
import praw
import requests
from dotenv import load_dotenv
import re
import time # <-- Added time module

# --- Environment Variables ---
CLIENT_ID = os.getenv("REDDIT_CLIENT_ID")
CLIENT_SECRET = os.getenv("REDDIT_CLIENT_SECRET")
USER_AGENT = os.getenv("REDDIT_USER_AGENT")
USERNAME = os.getenv("REDDIT_USERNAME")
PASSWORD = os.getenv("REDDIT_PASSWORD")
DOWNLOAD_LOCATION = os.getenv("DOWNLOAD_LOCATION", "./downloads")


def download_file(url, filename, check_size=False):
    """Downloads a file from a URL to a specified path."""
    filepath = os.path.join(DOWNLOAD_LOCATION, filename)
    if os.path.exists(filepath):
        if not check_size:
            # print(f"Skipped: {filename} already exists.")
            return

        try:
            head_response = requests.head(url, allow_redirects=True)
            remote_size = int(head_response.headers.get('content-length', 0))
            local_size = os.path.getsize(filepath)
            
            if remote_size > 0 and remote_size == local_size:
                print(f"Skipped: {filename} already exists and size matches.")
                return
            
            print(f"Redownloading {filename}: Local size {local_size} vs Remote size {remote_size}")
        except Exception as e:
            print(f"Error checking size for {filename}: {e}")
            return

    try:
        response = requests.get(url, stream=True)
        response.raise_for_status()  # Raise an exception for bad status codes
        with open(filepath, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)
        print(f"Downloaded: {filename}")
    except requests.exceptions.RequestException as e:
        print(f"Error downloading {url}: {e}")

def main():
    """Main function to download media from saved Reddit posts."""
    # Create download directory if it doesn't exist
    if not os.path.exists(DOWNLOAD_LOCATION):
        os.makedirs(DOWNLOAD_LOCATION)

    # Load environment variables just before use in main logic
    load_dotenv()
    
    # Authenticate with Reddit
    reddit = praw.Reddit(
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        user_agent=USER_AGENT,
        username=USERNAME,
        password=PASSWORD,
    )

    print("Successfully authenticated with Reddit.")
    
    deleted_redgifs_count = 0

    # Get saved posts
    saved_posts = reddit.user.me().saved(limit=None)

    for post in saved_posts:
        title = post.title
        # Sanitize the title to use as a filename
        sanitized_title = re.sub(r'[\\/*?:"<>|]', "", title)

        # --- Handle Galleries ---
        if hasattr(post, "is_gallery") and post.is_gallery:
            gallery_items = post.gallery_data['items']
            for i, item in enumerate(gallery_items):
                media_id = item['media_id']
                media_type = post.media_metadata[media_id]['m'].split('/')[-1]
                image_url = f"https://i.redd.it/{media_id}.{media_type}"
                filename = f"{sanitized_title}_{i+1}.{media_type}"
                download_file(image_url, filename)
            continue

        # --- Handle i.redd.it and i.imgur.com images/gifs ---
        if "i.redd.it" in post.url or "i.imgur.com" in post.url:
            file_extension = post.url.split('.')[-1]
            if file_extension in ['jpg', 'jpeg', 'png', 'gif']:
                 filename = f"{sanitized_title}.{file_extension}"
                 download_file(post.url, filename)
            continue
            
        # --- Handle RedGifs ---
        if "redgifs.com" in post.url:
            try:
                # Extract ID
                rg_match = re.search(r'redgifs\.com/(?:watch/|ifr/)?([a-zA-Z0-9]+)', post.url)
                if not rg_match:
                    print(f"Could not parse RedGifs ID from: {post.url}")
                    continue
                
                video_id = rg_match.group(1)
                
                # Get Temp Token
                auth_url = "https://api.redgifs.com/v2/auth/temporary"
                headers = {"User-Agent": "Mozilla/5.0"}
                auth_resp = requests.get(auth_url, headers=headers)
                auth_resp.raise_for_status()
                token = auth_resp.json()['token']
                
                # Get GIF Metadata
                meta_url = f"https://api.redgifs.com/v2/gifs/{video_id}"
                headers['Authorization'] = f"Bearer {token}"
                meta_resp = requests.get(meta_url, headers=headers)
                meta_resp.raise_for_status()
                meta_data = meta_resp.json()
                
                # Extract HD URL
                hd_url = meta_data.get('gif', {}).get('urls', {}).get('hd')
                
                if hd_url:
                    filename = f"{sanitized_title}.mp4"
                    download_file(hd_url, filename, check_size=True)
                else:
                    print(f"No HD URL found for RedGif: {post.url}")

            except requests.exceptions.HTTPError as e:
                if e.response.status_code == 410:
                    deleted_redgifs_count += 1
                else:
                    print(f"Error processing RedGif {post.url}: {e}")
            except Exception as e:
                print(f"Error processing RedGif {post.url}: {e}")
            continue

    if deleted_redgifs_count > 0:
        print(f"Skipped {deleted_redgifs_count} deleted RedGifs this session.")

# --- Main execution loop ---
if __name__ == "__main__":
    while True:
        print("-------------------------------------------")
        print("Starting new download cycle...")
        try:
            main()
        except Exception as e:
            print(f"An unexpected error occurred: {e}")
            print("Will retry after the delay.")
        
        print("\nDownload cycle finished. Waiting for 1 hour...")
        print("-------------------------------------------\n")
        # Wait for 1 hour (3600 seconds) before the next run
        time.sleep(3600)
