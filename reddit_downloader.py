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
TIME_BETWEEN_DOWNLOADS = int(os.getenv("TIME_BETWEEN_DOWNLOADS", "3600"))  # in seconds
CONSECUTIVE_SKIP_LIMIT = int(os.getenv("CONSECUTIVE_SKIP_LIMIT", "0"))

class RedGifsClient:
    def __init__(self):
        self.session = requests.Session()
        # Use a real browser User-Agent
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        })
        self.token = None

    def _authenticate(self):
        """Fetches a new temporary token."""
        try:
            auth_url = "https://api.redgifs.com/v2/auth/temporary"
            response = self.session.get(auth_url, timeout=20)
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
            response = self.session.get(meta_url, headers=headers, timeout=20)
            if response.status_code == 401:
                # Token might be expired, refresh and retry once
                logger.debug("RedGifs token expired, refreshing...")
                self._authenticate()
                if self.token:
                    headers['Authorization'] = f'Bearer {self.token}'
                    response = self.session.get(meta_url, headers=headers, timeout=20)
            
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


def download_file(url, filename, session=None, check_size=False):
    """
    Downloads a file from a URL.
    Uses 'session' if provided, otherwise uses standard requests.
    """
    filepath = os.path.join(DOWNLOAD_LOCATION, filename)
    
    # Use the specific session or fallback to requests
    requester = session if session else requests

    logger.trace(f"Checking file: {filename}")

    if os.path.exists(filepath):
        if not check_size:
            logger.trace(f"Skipped: {filename} already exists.")
            return True

        try:
            head_response = requester.head(url, allow_redirects=True, timeout=15)
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
        response = requester.get(url, stream=True, timeout=60)
        response.raise_for_status()
        with open(filepath, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)
        logger.trace(f"Downloaded: {filename}")
    except requests.exceptions.RequestException as e:
        logger.error(f"Error downloading {url}: {e}")
    
    return False

def main():
    """Main function to download media from saved Reddit posts."""
    if not os.path.exists(DOWNLOAD_LOCATION):
        os.makedirs(DOWNLOAD_LOCATION)
    
    # Configure shared session with retries
    session = requests.Session()
    # Good practice: ignore env proxies to prevent accidental interference
    session.trust_env = False 

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
    
    redgifs_client = RedGifsClient()
    
    deleted_redgifs_count = 0
    skipped_files_count = 0
    consecutive_skipped_count = 0

    saved_posts = reddit.user.me().saved(limit=None)

    for post in saved_posts:
        title = post.title
        sanitized_title = re.sub(r'[\\/*?:"<>|]', "", title)

        # --- Handle Galleries ---
        if hasattr(post, "is_gallery") and post.is_gallery:
            gallery_items = post.gallery_data['items']
            for i, item in enumerate(gallery_items):
                media_id = item['media_id']
                media_type = post.media_metadata[media_id]['m'].split('/')[-1]
                image_url = f"https://i.redd.it/{media_id}.{media_type}"
                filename = f"{sanitized_title}_{i+1}.{media_type}"
                # Use the main session (efficient)
                if download_file(image_url, filename, session=session):
                    skipped_files_count += 1
                    consecutive_skipped_count += 1
                else:
                    consecutive_skipped_count = 0
                
                if CONSECUTIVE_SKIP_LIMIT > 0 and consecutive_skipped_count >= CONSECUTIVE_SKIP_LIMIT:
                    logger.info(f"Consecutive skip limit ({CONSECUTIVE_SKIP_LIMIT}) reached. Stopping download session.")
                    return
            continue

        # --- Handle i.redd.it and i.imgur.com ---
        if "i.redd.it" in post.url or "i.imgur.com" in post.url:
            file_extension = post.url.split('.')[-1]
            if file_extension in ['jpg', 'jpeg', 'png', 'gif']:
                 filename = f"{sanitized_title}.{file_extension}"
                 # Use the main session
                 if download_file(post.url, filename, session=session):
                     skipped_files_count += 1
                     consecutive_skipped_count += 1
                 else:
                     consecutive_skipped_count = 0
                 
                 if CONSECUTIVE_SKIP_LIMIT > 0 and consecutive_skipped_count >= CONSECUTIVE_SKIP_LIMIT:
                     logger.info(f"Consecutive skip limit ({CONSECUTIVE_SKIP_LIMIT}) reached. Stopping download session.")
                     return
            continue
            
        # --- Handle RedGifs ---
        if "redgifs.com" in post.url:
            try:
                rg_match = re.search(r'redgifs\.com/(?:watch/|ifr/)?([a-zA-Z0-9]+)', post.url)
                if not rg_match:
                    logger.warning(f"Could not parse RedGifs ID from: {post.url}")
                    continue
                
                video_id = rg_match.group(1)
                
                # Get GIF Metadata
                meta_data = redgifs_client.get_media_info(video_id)
                if not meta_data:
                    continue

                hd_url = meta_data.get('gif', {}).get('urls', {}).get('hd')
                
                if hd_url:
                    filename = f"{sanitized_title}.mp4"
                    # Use the main session for the download
                    if download_file(hd_url, filename, session=session, check_size=True):
                        skipped_files_count += 1
                        consecutive_skipped_count += 1
                    else:
                        consecutive_skipped_count = 0
                    
                    if CONSECUTIVE_SKIP_LIMIT > 0 and consecutive_skipped_count >= CONSECUTIVE_SKIP_LIMIT:
                        logger.info(f"Consecutive skip limit ({CONSECUTIVE_SKIP_LIMIT}) reached. Stopping download session.")
                        return
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
        time.sleep(TIME_BETWEEN_DOWNLOADS)
