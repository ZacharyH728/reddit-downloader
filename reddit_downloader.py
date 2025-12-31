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

"""Sends a POST request to the Stash instance to scan for new media."""
def scanStash():
    url = "https://stash.zhill.me/graphql"
    headers = {
        "ApiKey": "TEST",
        "Content-Type": "application/json"
    }
    data = {
        "query": "{ metadataScan }"
    }

    try:
        response = requests.post(url, headers=headers, json=data)
        response.raise_for_status()  # Raise an exception for HTTP errors (4xx or 5xx)
        print("Status Code:", response.status_code)
        print("Response Body:", response.json())
    except requests.exceptions.RequestException as e:
        print(f"An error occurred: {e}")

def download_file(url, filename):
    """Downloads a file from a URL to a specified path."""
    filepath = os.path.join(DOWNLOAD_LOCATION, filename)
    if os.path.exists(filepath):
        print(f"Skipped: {filename} already exists.")
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
                # A simple way to get the video URL is to fetch the page and find it
                response = requests.get(post.url)
                response.raise_for_status()
                # Find the mp4 URL in the page content
                match = re.search(r'https?://[^\s"]+\.mp4', response.text)
                if match:
                    video_url = match.group(0)
                    filename = f"{sanitized_title}.mp4"
                    download_file(video_url, filename)
                else:
                    print(f"Could not find .mp4 for RedGif: {post.url}")
            except requests.exceptions.RequestException as e:
                print(f"Error fetching RedGif page {post.url}: {e}")
            continue

# --- Main execution loop ---
if __name__ == "__main__":
    while True:
        print("-------------------------------------------")
        print("Starting new download cycle...")
        try:
            main()
            scanStash()
        except Exception as e:
            print(f"An unexpected error occurred: {e}")
            print("Will retry after the delay.")
        
        print("\nDownload cycle finished. Waiting for 1 hour...")
        print("-------------------------------------------\n")
        # Wait for 1 hour (3600 seconds) before the next run
        time.sleep(3600)
