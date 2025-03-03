import requests
from bs4 import BeautifulSoup
import json
from concurrent.futures import ThreadPoolExecutor, as_completed, ProcessPoolExecutor
import subprocess
from pymongo import MongoClient, errors
from tenacity import retry, wait_fixed, stop_after_attempt, retry_if_exception_type, RetryError
from dataclasses import dataclass, field
from typing import List, Optional, Dict
from datetime import datetime
import math

# Define the Article dataclass
@dataclass
class Article:
    url: str
    postid: Optional[str] = None
    title: Optional[str] = None
    keywords: List[str] = field(default_factory=list)
    thumbnail: Optional[str] = None
    video_duration: Optional[str] = None
    word_count: Optional[str] = None
    lang: Optional[str] = None
    published_time: Optional[str] = None
    last_updated: Optional[str] = None
    description: Optional[str] = None
    author: Optional[str] = None
    classes: List[Dict] = field(default_factory=list)
    text: Optional[str] = None
    filename: Optional[str] = None

# Function to fetch and parse a sitemap with retry logic
@retry(wait=wait_fixed(2), stop=stop_after_attempt(5), retry=retry_if_exception_type(requests.RequestException))
def fetch_sitemap(url):
    response = requests.get(url)
    response.raise_for_status()
    return BeautifulSoup(response.content, 'xml')

# Function to count articles in a given sitemap URL with retry logic
def count_articles_in_sitemap(sitemap_url):
    try:
        sitemap_soup = fetch_sitemap(sitemap_url)
        article_urls = [loc.text for loc in sitemap_soup.find_all("loc")]
        return len(article_urls), article_urls
    except RetryError as e:
        print(f"Skipping sitemap {sitemap_url} after failed attempts: {e}")
        return 0, []

# Function to fetch and parse an article with retry logic
@retry(wait=wait_fixed(2), stop=stop_after_attempt(5), retry=retry_if_exception_type(requests.RequestException))
def fetch_article(article_url, filename):
    try:
        article_response = requests.get(article_url)
        article_response.raise_for_status()
        article_soup = BeautifulSoup(article_response.content, 'html.parser')

        script_tag = article_soup.find('script', type='text/tawsiyat')
        metadata = {}
        if script_tag:
            metadata = json.loads(script_tag.string.strip())

        # Convert keywords to list if it's a string
        keywords = metadata.get("keywords", "").split(',') if isinstance(metadata.get("keywords", ""), str) else metadata.get("keywords", [])

        paragraphs = article_soup.find_all('p')
        article_text = '\n'.join([p.get_text(strip=True) for p in paragraphs])

        article = Article(
            url=metadata.get("url", article_url),
            postid=metadata.get("postid"),
            title=metadata.get("title"),
            keywords=keywords,
            thumbnail=metadata.get("thumbnail"),
            video_duration=metadata.get("video_duration"),
            word_count=metadata.get("word_count"),
            lang=metadata.get("lang"),
            published_time=metadata.get("published_time"),
            last_updated=metadata.get("last_updated"),
            description=metadata.get("description"),
            author=metadata.get("author"),
            classes=metadata.get("classes", []),
            text=article_text,
            filename=filename
        )

        return article

    except RetryError as e:
        print(f"Skipping article {article_url} after failed attempts: {e}")
        return None

# Function to get the number of CPU cores
def get_cpu_cores():
    try:
        result = subprocess.run(['nproc'], stdout=subprocess.PIPE, text=True)
        return int(result.stdout.strip())
    except Exception as e:
        print(f"Failed to get number of CPU cores: {e}")
        return 1  # Default to 1 if there is an error

# MongoDB setup
client = MongoClient('mongodb://localhost:27017/')  # Update with your MongoDB connection string if needed
db = client['articles_db']  # Updated database name

# Ensure the unique index is created on the 'url' field
collection = db['articles']  # Collection name
try:
    collection.create_index([('url', 1)], unique=True)
    print("Index created successfully.")
except errors.DuplicateKeyError as e:
    print(f"Index creation failed: {e}")

# Function to get the year and month for MongoDB document naming
def extract_year_month(sitemap_url):
    year_month = sitemap_url.split('-')[-2:]
    year = year_month[0]
    month = year_month[1].split('.')[0]
    return year, month

# Step 1: Retrieve monthly sitemaps from the main sitemap index
sitemap_index_url = "https://www.almayadeen.net/sitemaps/all.xml"
try:
    sitemap_soup = fetch_sitemap(sitemap_index_url)
    monthly_sitemaps = [loc.text for loc in sitemap_soup.find_all("loc")]
except Exception as e:
    print(f"Failed to fetch sitemap index: {e}")
    monthly_sitemaps = []

# Automatically determine the current year and month
current_year = datetime.now().year
current_month = datetime.now().month

# Global counters
total_articles_saved = 0

# Function to check if the data for the current month already exists in MongoDB
def check_data_exists(year, month):
    return collection.count_documents({"published_time": {"$regex": f"^{year}-{month:02}"}}) > 0

# Function to process a single article chunk
def process_article_chunk(article_urls_chunk, filename):
    global total_articles_saved

    with ThreadPoolExecutor(max_workers=get_cpu_cores()) as executor:
        futures = {executor.submit(fetch_article, url, filename): url for url in article_urls_chunk}
        for future in as_completed(futures):
            try:
                article = future.result()
                if article:
                    try:
                        collection.insert_one(article.__dict__)
                        total_articles_saved += 1
                    except errors.DuplicateKeyError:
                        print(f"Duplicate article found, skipping: {article.url}")
            except Exception as e:
                print(f"Error processing article: {e}")

# Function to process a single sitemap
def process_sitemap(sitemap_url):
    global total_articles_saved

    # Extract the year and month from the sitemap URL for MongoDB document naming
    year, month = extract_year_month(sitemap_url)

    # Skip if the year or month does not match the current one
    if int(year) != current_year or int(month) != current_month:
        return

    # Skip if data for the current month already exists in MongoDB
    if check_data_exists(year, month):
        print(f"Data for {year}-{month:02} already exists in MongoDB. Skipping.")
        return

    # Count articles in the sitemap and get the URLs
    total_articles_in_sitemap, article_urls = count_articles_in_sitemap(sitemap_url)
    if total_articles_in_sitemap == 0:
        print(f"Skipping sitemap {sitemap_url} due to failure in counting articles.")
        return

    print(f"Sitemap URL: {sitemap_url}, Number of articles: {total_articles_in_sitemap}")

    filename = f"articles_{year}_{month}"

    # Split the article URLs into chunks based on the number of CPU cores
    num_cores = get_cpu_cores()
    chunk_size = math.ceil(total_articles_in_sitemap / num_cores)
    article_chunks = [article_urls[i:i + chunk_size] for i in range(0, len(article_urls), chunk_size)]

    # Process each chunk concurrently using ProcessPoolExecutor
    with ProcessPoolExecutor(max_workers=num_cores) as executor:
        executor.map(process_article_chunk, article_chunks, [filename]*len(article_chunks))

# Process each sitemap
with ProcessPoolExecutor(max_workers=get_cpu_cores()) as executor:
    executor.map(process_sitemap, monthly_sitemaps)

print(f"Total articles saved to MongoDB: {total_articles_saved}")
