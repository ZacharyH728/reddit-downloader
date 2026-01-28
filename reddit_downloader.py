import os
import praw
import prawcore
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from dotenv import load_dotenv
import re
import time
import logging
import sys

load_dotenv()

# --- Logging Setup ---
TRACE_LEVEL_NUM = 5
logging.addLevelName(TRACE_LEVEL_NUM, "TRACE")

def trace(self, message, *args, **kws):
    if self.isEnabledFor(TRACE_LEVEL_NUM):
        self._log(TRACE_LEVEL_NUM, message, args, **kws)

logging.Logger.trace = trace

# Get log level from environment, default to INFO
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
numeric_level = getattr(logging, LOG_LEVEL, logging.INFO)
# If the user sets a custom level that doesn't exist, fallback to INFO, 
# but if they explicitly want TRACE (which isn't standard in logging module dict yet unless added), handle it.
if LOG_LEVEL == "TRACE":
    numeric_level = TRACE_LEVEL_NUM

logging.basicConfig(
    level=numeric_level,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)

logger = logging.getLogger(__name__)

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
        # Use a real browser User-Agent to avoid generic bot detection
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        })
        self.token = None

    def _authenticate(self):
        """Fetches a new temporary token."""
        try:
            auth_url = "https://api.redgifs.com/v2/auth/temporary"
            response = self.session.get(auth_url)
            response.raise_for_status()
            data = response.json()
            self.token = data.get('token')
            logger.trace("Successfully acquired new RedGifs token.")
        except Exception as e:
            logger.error(f"Failed to authenticate with RedGifs: {e}")
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
                logger.debug("RedGifs token expired, refreshing...")
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
            logger.error(f"Error fetching RedGifs metadata for {video_id}: {e}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error fetching RedGifs metadata for {video_id}: {e}")
            return None


def download_file(url, filename, check_size=False):
    """Downloads a file from a URL to a specified path. Returns True if skipped."""
    filepath = os.path.join(DOWNLOAD_LOCATION, filename)
    
    # Log the file we are checking at TRACE level
    logger.trace(f"Checking file: {filename}")

    if os.path.exists(filepath):
        if not check_size:
            logger.trace(f"Skipped: {filename} already exists.")
            return True

        try:
            head_response = requests.head(url, allow_redirects=True)
            remote_size = int(head_response.headers.get('content-length', 0))
            local_size = os.path.getsize(filepath)
            
            if remote_size > 0 and remote_size == local_size:
                logger.trace(f"Skipped: {filename} already exists and size matches.")
                return True
            
            logger.info(f"Redownloading {filename}: Local size {local_size} vs Remote size {remote_size}")
        except Exception as e:
            logger.error(f"Error checking size for {filename}: {e}")
            return False

    try:
        response = requests.get(url, stream=True)
        response.raise_for_status()  # Raise an exception for bad status codes
        with open(filepath, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)
        # Log successful download at TRACE level (per user request to put download status on trace)
        logger.trace(f"Downloaded: {filename}")
    except requests.exceptions.RequestException as e:
        logger.error(f"Error downloading {url}: {e}")
    
    return False

def main():
    """Main function to download media from saved Reddit posts."""
    # Create download directory if it doesn't exist
    if not os.path.exists(DOWNLOAD_LOCATION):
        os.makedirs(DOWNLOAD_LOCATION)
    
    # Configure session with retries
    session = requests.Session()
    retries = Retry(
        total=5,
        backoff_factor=1,
        status_forcelist=[500, 502, 503, 504],
        allowed_methods=["HEAD", "GET", "OPTIONS", "POST"]
    )
    adapter = HTTPAdapter(max_retries=retries)
    session.mount("https://", adapter)
    session.mount("http://", adapter)

    # Authenticate with Reddit
    reddit = praw.Reddit(
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        user_agent=USER_AGENT,
        username=USERNAME,
        password=PASSWORD,
        requestor_kwargs={'session': session}
    )

    logger.info("Successfully authenticated with Reddit.")
    
    # Initialize RedGifs Client
    redgifs_client = RedGifsClient()
    
    deleted_redgifs_count = 0
    skipped_files_count = 0

    # Get saved posts
    # Note: Fetching saved posts can take time, might want a log here if it blocks, but leaving as is.
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
                    logger.warning(f"Could not parse RedGifs ID from: {post.url}")
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
                    logger.warning(f"No HD URL found for RedGif: {post.url}")

            except requests.exceptions.HTTPError as e:
                if e.response.status_code == 410:
                    deleted_redgifs_count += 1
                else:
                    logger.error(f"Error processing RedGif {post.url}: {e}")
            except Exception as e:
                logger.error(f"Error processing RedGif {post.url}: {e}")
            continue

    if deleted_redgifs_count > 0:
        logger.info(f"Skipped {deleted_redgifs_count} deleted RedGifs this session.")
    
    if skipped_files_count > 0:
        logger.info(f"Skipped {skipped_files_count} files that already existed.")

# --- Main execution loop ---
if __name__ == "__main__":
    while True:
        logger.info("-------------------------------------------")
        logger.info("Starting new download cycle...")
        try:
            main()
        except (prawcore.exceptions.RequestException, requests.exceptions.RequestException) as e:
            logger.error(f"Connection error occurred: {e}")
            logger.info("Will retry in 60 seconds...")
            time.sleep(60)
            continue
        except Exception as e:
            logger.exception(f"An unexpected error occurred: {e}")
            logger.info("Will retry after the delay.")
        
        logger.info("Download cycle finished. Waiting for 1 hour...")
        logger.info("-------------------------------------------\n")
        # Wait for 1 hour (3600 seconds) before the next run
        time.sleep(3600)
