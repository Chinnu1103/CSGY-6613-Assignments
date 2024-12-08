from langchain_community.document_transformers import Html2TextTransformer
from webdriver_manager.chrome import ChromeDriverManager
from youtube_transcript_api import YouTubeTranscriptApi
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from langchain.docstore.document import Document
from selenium.webdriver.common.keys import Keys
from youtube_search import YoutubeSearch
from selenium.webdriver.common.by import By
from pymongo import MongoClient
from selenium import webdriver
from bs4 import BeautifulSoup
from github import Github
from tqdm import tqdm
import subprocess
import requests
import tempfile
import logging
import time
import json
import glob
import os

#Class for storing the common clients and objects
class BaseETLPipeline:
    def __init__(self, mongodb_uri, db_name):
        self.client = MongoClient(mongodb_uri)
        self.db = self.client[db_name]
        self.html2text = Html2TextTransformer()

class GitHubPipeline(BaseETLPipeline):
    def __init__(self, mongodb_uri, db_name, github_token):
        super().__init__(mongodb_uri, db_name)
        self.github = Github(github_token)
        self.collection = self.db["GitHub"]
        self.collection.delete_many({})
    
    def parse_reponame(self, url):
        #Extract repository name from the GitHub URL.
        parts = url.split("/")
        return f"{parts[3]}/{parts[4]}"
    
    def fetch_all_files(self, repo, branch, directory="source", temp_dir=".", file_type=".rst"):
        #Fetch files from the given directory in a branch.
        contents = repo.get_contents(directory, ref=branch)
        all_files = []
        
        for item in contents:
            if item.type == "dir":
                # Recursively fetch files from subdirectories
                os.makedirs(os.path.join(temp_dir, item.path), exist_ok=True)
                all_files.extend(self.fetch_all_files(repo, branch, item.path, temp_dir, file_type))
            elif item.type == "file" and item.name.endswith(file_type):
                file_path = os.path.join(temp_dir, item.path)
                with open(file_path, "w", encoding="utf-8") as curr_file:
                    curr_file.write(requests.get(item.download_url).text) 
                all_files.append(item)
        
        return all_files
    
    def parse_rst(self, temp_dir):
        #Parse RST content to HTML. usingsphinx
        src_dir = temp_dir
        out_dir = os.path.join(temp_dir, "build")
        os.makedirs(out_dir, exist_ok=True)
        logging.info("Converting RST Files to HTML")
        # Configure and run Sphinx
        try:
            command = [
                'sphinx-build',
                '-b', 'html',
                src_dir,
                out_dir
            ]
            with open('sphinx_build.log', 'w') as log_file:
                subprocess.run(command, stdout=log_file, stderr=log_file)

        except Exception as e:
            pass
        
        #Fetch the html content from all converted files
        logging.info("Completed Conversion")
        content_dict = {}
        for filename in tqdm(glob.glob(os.path.join(out_dir, "**", "*.html"), recursive=True), "Reading and converting HTML", leave=False):
            file_path = os.path.join(out_dir, filename)
            with open(file_path, "r", encoding="utf-8") as f:
                html_content = f.read()
                doc =  Document(page_content=html_content, metadata={"source": "local"})
                file_content = self.html2text.transform_documents([doc])[0].page_content
            
            content_dict[filename] = file_content
        
        return content_dict
    
    def parse_md(self, temp_dir):
        #Fetch markdown content from a directory
        content_dict = {}
        for filename in tqdm(glob.glob(os.path.join(temp_dir, "**", "*.md"), recursive=True), "Reading Markdown", leave=False):
            file_path = os.path.join(temp_dir, filename)
            with open(file_path, "r", encoding="utf-8") as f:
                file_content = f.read()
                
            content_dict[filename] = file_content
        
        return content_dict

    def extract_and_transform_and_load(self):
        #Function to get all RST files from all ROS2 documentation urls
        logging.info("Starting to extract GitHub repositories")
        with open('ros_github_urls.json', 'r') as file:
            data = json.load(file)
        
        for source in data:
            repo_name = self.parse_reponame(source["url"])
            branch = source["branch"]
            
            logging.info(f"Processing source: {repo_name}")
            repo = self.github.get_repo(repo_name)
            
            try:
                with tempfile.TemporaryDirectory() as temp_dir:
                    logging.info(f"Temporary directory created - {temp_dir}")
                    os.makedirs(temp_dir, exist_ok=True)
                    
                    logging.info(f"Fetching all files in repository") 
                    if source["doc_path"] != ".":
                        os.makedirs(os.path.join(temp_dir, source["doc_path"]), exist_ok=True)
                    all_files = self.fetch_all_files(repo, branch, source["doc_path"], temp_dir, source["type"])
                    logging.info(f"Found {len(all_files)} files in current repo")                   
                    
                    if source["type"] == ".rst":
                        src_dir = temp_dir
                        if source["doc_path"] != ".":
                            src_dir = os.path.join(src_dir, source["doc_path"])
                        conf_py = os.path.join(src_dir, "conf.py")
                        with open(conf_py, "w", encoding="utf-8") as conf_file:
                            conf_file.write(f"extensions = []\nmaster_doc = 'index'\nsuppress_warnings = ['*']\n")
                        parsed_contents = self.parse_rst(src_dir)
                    else:
                        parsed_contents = self.parse_md(temp_dir)
                
                    for file_path, file_content in tqdm(parsed_contents.items(), "Inserting Data", leave=True):   
                        url = source["url"] + "/"
                        if source["type"] == ".rst":
                            base_path = os.path.relpath(file_path, os.path.join(temp_dir, "build"))
                            base_path = os.path.splitext(base_path)[0] + ".rst"
                        else:
                            base_path = os.path.relpath(file_path, temp_dir)
                        
                        url += base_path
                            
                        doc = {
                            "repo": repo_name,
                            "branch": branch,
                            "url": url,
                            "data": file_content,
                        }
                        self.collection.insert_one(doc)
                
            except Exception as e:
                logging.error(f"Error processing branch - {branch}", e)

class YouTubePipeline(BaseETLPipeline):
    
    def __init__(self, mongodb_uri, db_name, search_terms=None):
        super().__init__(mongodb_uri, db_name)
        self.collection = self.db["YouTube"]
        self.collection.delete_many({})
        
        # Default search terms if not provided
        self.search_terms = search_terms or [
            "ROS2 Tutorial",
            "ROS Robot Operating System Documentation",
            "ROS Programming Explained",
            "ROS Robotics Tutorials",
            "ROS Navigation Stack",
            "ROS Perception Tutorial"
        ]
        self.urls = set()

    def search_youtube_videos(self, search_term, max_results=20):
        """
        Search YouTube for videos related to a search term

        :param search_term: Term to search for
        :param max_results: Maximum number of results to retrieve
        :return: List of video details
        """
        try:
            search_result = YoutubeSearch(search_term, max_results=max_results).to_dict()
            
            videos = []
            
            for video in search_result:
                if video['url_suffix'] not in self.urls:
                    videos.append({
                        "title": video['title'],
                        "video_id": video['id'],
                        "channel": video['channel'],
                        "url": video['url_suffix']
                    })
                    self.urls.add(video['url_suffix'])
            
            return videos
        
        except Exception as e:
            logging.error(f"Error searching YouTube for term '{search_term}': {e}")
            return []

    def get_video_transcript(self, video_id):
        """
        Retrieve transcript for a given YouTube video

        :param video_id: YouTube video ID
        :return: Transcript text or None
        """
        try:
            transcript = YouTubeTranscriptApi.get_transcript(video_id, languages=['en'])
            
            # Combine transcript with timestamps
            full_transcript = " ".join([
                entry['text'] for entry in transcript
            ])
            
            return full_transcript
        
        except Exception as e:
            logging.debug(f"Could not retrieve transcript for video {video_id}: {e}")
            return None

    def extract_and_transform_and_load(self):
        """
        Main ETL method to search videos, get transcripts, and store in MongoDB
        """
        logging.info("Starting YouTube ROS Documentation Transcript Pipeline")
        
        for search_term in tqdm(self.search_terms, desc="Search Terms"):
            logging.info(f"Searching YouTube for: {search_term}")
            
            # Search videos
            videos = self.search_youtube_videos(search_term)
            
            # Process each video
            i = 0
            for video in tqdm(videos, desc=f"Processing Videos for {search_term}", leave=False):
                try:
                    # Get transcript
                    transcript = self.get_video_transcript(video['video_id'])
                    if transcript:
                        # Prepare document for MongoDB
                        doc = {
                            "video_id": video['video_id'],
                            "video_title": video['title'],
                            "url": "https://www.youtube.com" + video['url'],
                            "channel": video['channel'],
                            "search_term": search_term,
                            "transcript": transcript
                        }
                        
                        # Insert into MongoDB
                        self.collection.insert_one(doc)
                        i += 1
                
                except Exception as e:
                    logging.error(f"Error processing video {video['video_id']}: {e}")
                
            logging.info(f"Processed {i} videos for term - {search_term}")

class MediumPipeline(BaseETLPipeline):
    SCROLL_PAUSE_TIME = 3
    SCROLL_COUNT = 20
    
    def __init__(self, mongodb_uri, db_name):
        super().__init__(mongodb_uri, db_name)
        self.collection = self.db["Medium"]
        self.articles_url = "https://medium.com/tag/ros2/recommended"
        self.collection.delete_many({})

    def scrape_medium_articles(self):
        options = Options()
        driver = webdriver.Remote("http://selenium:4444", options=options)
        driver.get(self.articles_url)
        
        logging.info("Scrolling page")
        for _ in tqdm(range(self.SCROLL_COUNT), "Scroll count"):
            # Scroll down the page
            driver.find_element(By.TAG_NAME, 'body').send_keys(Keys.END)
            time.sleep(self.SCROLL_PAUSE_TIME)
        
        soup = BeautifulSoup(driver.page_source, "html.parser")
        articles = soup.find_all('article')
        
        article_data = []
        for article in articles:
            link = article.find('a', {"class": "af ag ah ai aj ak al am an ao ap aq ar as at"})['href']
            article_data.append({"url":"https://medium.com" + link})
        
        for article in tqdm(article_data, "Article Parsing Progress"):
            article_content = requests.get(article["url"]).text
            soup = BeautifulSoup(article_content, "html.parser")
            title = soup.find('h1')
            if title is not None:
                article["name"] = title.get_text(strip=True)
            else:
                article["name"] = "Unknown"
                
            article["data"] = article_content
        
        driver.quit()
        
        return article_data
    
    def extract_and_transform_and_load(self):
        articles = self.scrape_medium_articles()
        
        for article in tqdm(articles, "Data Insert"):   
            doc = Document(page_content=article["data"], metadata={"source": "local"})  
            article["data"] =  self.html2text.transform_documents([doc])[0].page_content
            doc = {
                "name": article["name"],
                "url": article["url"],
                "data": article["data"]
            }
            
            # Insert into MongoDB
            self.collection.insert_one(doc)

class LinkedInPipeline(BaseETLPipeline):
    def __init__(self, mongodb_uri, db_name):
        super().__init__(mongodb_uri, db_name)
        self.collection = self.db["LinkedIn"]
        self.articles_url = "https://www.linkedin.com/pulse/topics/robot-operating-system-(ros)-s26434/"
        self.allowed_classes = ["article-main__content", "contribution__text"]
        self.collection.delete_many({})

    def scrape_linkedin_articles(self):
        # Initialize Selenium
        options = Options()
        driver = webdriver.Remote("http://selenium:4444", options=options)
        driver.get(self.articles_url)

        # Parse the page with BeautifulSoup
        soup = BeautifulSoup(driver.page_source, "html.parser")
        driver.quit()

        # Extract article links
        article_divs = soup.find_all("div", {"class": "ml-1"})
        articles = []
        for div in article_divs:
            elem = div.find("a", {"class": "content-hub-entities"})
            url = elem['href']
            articles.append({"url": url if url.startswith("https://") else f"https://www.linkedin.com{url}"})

        return articles
    
    def scrape_article_content(self, article_url):
        response = requests.get(article_url).text
        soup = BeautifulSoup(response, "html.parser")

        # Extract the article name and content
        title = soup.find('h1', {"class": 'pulse-title'})

        return {
            "name": title.get_text(strip=True) if title else "Untitled",
            "data": response,
        }
    
    def filter_text_content(self, html_str):
        soup = BeautifulSoup(html_str, 'html.parser')
        filtered_tags = [
            tag for tag in soup.find_all(class_=self.allowed_classes)
            if not ("contribution__see-more-button" in tag.get("class", []))
        ]
        filtered_tags = soup.find_all(class_=self.allowed_classes)
        for tag in filtered_tags:
            buttons_to_remove = tag.find_all("button", class_="contribution__see-more-button")
            for button in buttons_to_remove:
                button.decompose()

        filtered_html = " ".join([str(tag) for tag in filtered_tags])
        doc =  Document(page_content=filtered_html, metadata={"source": "local"})
        file_content = self.html2text.transform_documents([doc])[0].page_content
        
        return file_content

    def extract_and_transform_and_load(self):
        articles = self.scrape_linkedin_articles()

        for article in tqdm(articles, "Article Processing Progress"):
            content = self.scrape_article_content(article["url"])
            doc = {
                "name": content["name"],
                "url": article["url"],
                "data": self.filter_text_content(content["data"]),
            }
            # Insert into MongoDB
            self.collection.insert_one(doc)