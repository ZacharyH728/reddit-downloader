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


class RedGifsClient:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "Mozilla/5.0"})
        self.token = None

    def _authenticate(self):
        """Fetches a new temporary token."""
        try:
            auth_url = "https://api.redgifs.com/v2/auth/temporary"
            response = self.session.get(auth_url)
            response.raise_for_status()
            data = response.json()
            self.token = data.get('token')
            # print("Successfully acquired new RedGifs token.")
        except Exception as e:
            print(f"Failed to authenticate with RedGifs: {e}")
            self.token = None

    def get_media_info(self, video_id):
        """Fetches media metadata, handling token refresh on 401."""
        if not self.token:
            self._authenticate()
            if not self.token:
                return None

        meta_url = f"https://api.redgifs.com/v2/gifs/{video_id}"
        
        # First attempt
        headers = {'Authorization': f'Bearer {self.token}'}
        try:
            response = self.session.get(meta_url, headers=headers)
            if response.status_code == 401:
                # Token might be expired, refresh and retry once
                # print("RedGifs token expired, refreshing...")
                self._authenticate()
                if self.token:
                    headers['Authorization'] = f'Bearer {self.token}'
                    response = self.session.get(meta_url, headers=headers)
            
            response.raise_for_status()
            return response.json()
            
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 410:
                # Video deleted
                raise e 
            print(f"Error fetching RedGifs metadata for {video_id}: {e}")
            return None
        except Exception as e:
            print(f"Unexpected error fetching RedGifs metadata for {video_id}: {e}")
            return None


def download_file(url, filename, check_size=False):
    """Downloads a file from a URL to a specified path. Returns True if skipped."""
    filepath = os.path.join(DOWNLOAD_LOCATION, filename)
    if os.path.exists(filepath):
        if not check_size:
            # print(f"Skipped: {filename} already exists.")
            return True

        try:
            head_response = requests.head(url, allow_redirects=True)
            remote_size = int(head_response.headers.get('content-length', 0))
            local_size = os.path.getsize(filepath)
            
            if remote_size > 0 and remote_size == local_size:
                # print(f"Skipped: {filename} already exists and size matches.")
                return True
            
            print(f"Redownloading {filename}: Local size {local_size} vs Remote size {remote_size}")
        except Exception as e:
            print(f"Error checking size for {filename}: {e}")
            return False

    try:
        response = requests.get(url, stream=True)
        response.raise_for_status()  # Raise an exception for bad status codes
        with open(filepath, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)
        print(f"Downloaded: {filename}")
    except requests.exceptions.RequestException as e:
        print(f"Error downloading {url}: {e}")
    
    return False

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
    
    # Initialize RedGifs Client
    redgifs_client = RedGifsClient()
    
    deleted_redgifs_count = 0
    skipped_files_count = 0

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
                if download_file(image_url, filename):
                    skipped_files_count += 1
            continue

        # --- Handle i.redd.it and i.imgur.com images/gifs ---
        if "i.redd.it" in post.url or "i.imgur.com" in post.url:
            file_extension = post.url.split('.')[-1]
            if file_extension in ['jpg', 'jpeg', 'png', 'gif']:
                 filename = f"{sanitized_title}.{file_extension}"
                 if download_file(post.url, filename):
                     skipped_files_count += 1
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
                
                # Get GIF Metadata using the client
                meta_data = redgifs_client.get_media_info(video_id)
                if not meta_data:
                    # Errors are already logged inside get_media_info
                    continue

                # Extract HD URL
                hd_url = meta_data.get('gif', {}).get('urls', {}).get('hd')
                
                if hd_url:
                    filename = f"{sanitized_title}.mp4"
                    if download_file(hd_url, filename, check_size=True):
                        skipped_files_count += 1
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
    
    if skipped_files_count > 0:
        print(f"Skipped {skipped_files_count} files that already existed.")

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
