# ===============================
#  [1] Imports
# ===============================
import os
import time
import json
import re
import threading
import requests
import schedule
import sqlite3
from datetime import datetime
from bs4 import BeautifulSoup
from flask import Flask, render_template, request, jsonify, redirect, url_for

# ===============================
#  [2] Environment Config
# ===============================
app = Flask(__name__)

QBITTORRENT_HOST = os.environ.get('QBITTORRENT_HOST', 'localhost')
QBITTORRENT_PORT = os.environ.get('QBITTORRENT_PORT', '8880')
QBITTORRENT_USERNAME = os.environ.get('QBITTORRENT_USERNAME', 'admin')
QBITTORRENT_PASSWORD = os.environ.get('QBITTORRENT_PASSWORD', 'adminadmin')
DB_PATH = os.environ.get('DB_PATH', 'data/anime_watchlist.db')
# Add schedule configuration options
SCHEDULE_INTERVAL = int(os.environ.get('SCHEDULE_INTERVAL', 1))  # Default: check every 1 hour
SCHEDULE_UNIT = os.environ.get('SCHEDULE_UNIT', 'hour')  # Options: 'minute', 'hour', 'day'

# ===============================
#  [3] Database Setup
# ===============================
def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # Create main tables if they don't exist
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS anime (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            search_query TEXT NOT NULL,
            status TEXT DEFAULT 'watching',
            last_episode INTEGER DEFAULT 0,
            next_episode_date TEXT,
            auto_download BOOLEAN DEFAULT 1
        )
    ''')
    
    # Check if schedule_interval column exists, add it if not
    cursor.execute("PRAGMA table_info(anime)")
    columns = cursor.fetchall()
    column_names = [col[1] for col in columns]
    
    if 'schedule_interval' not in column_names:
        print("Adding schedule_interval column to anime table")
        cursor.execute('ALTER TABLE anime ADD COLUMN schedule_interval TEXT DEFAULT "global"')
    
    # Create other tables
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS downloads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            anime_id INTEGER,
            episode INTEGER,
            magnet_link TEXT,
            download_date TEXT,
            FOREIGN KEY (anime_id) REFERENCES anime (id)
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            anime_id INTEGER,
            task_type TEXT,
            status TEXT DEFAULT 'running',
            progress INTEGER DEFAULT 0,
            total_pages INTEGER DEFAULT 1,
            current_page INTEGER DEFAULT 1,
            created_at TEXT,
            updated_at TEXT,
            FOREIGN KEY (anime_id) REFERENCES anime (id)
        )
    ''')

    conn.commit()
    conn.close()

# ===============================
#  [4] Magnet Fetching & Torrent Add
# ===============================
def fetch_magnet_links(search_query, page=1):
    try:
        url = f"https://nyaa.si/?f=0&c=1_2&q={search_query}&p={page}"
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'
        }
        response = requests.get(url, headers=headers)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')

        results = []
        rows = soup.select('table.torrent-list > tbody > tr')
        
        for row in rows:
            title_el = row.select_one('td:nth-child(2) a:not(.comments)')
            if not title_el:
                continue
            title = title_el.text.strip()
            title_lower = title.lower()

            if any(batch in title_lower for batch in ['batch', 'complete', '01-', '-complete']):
                continue

            is_movie = 'movie' in title_lower or 'film' in title_lower
            title_for_search = re.sub(r'\[\w{8}\]', '', title_lower)  # Remove any hashes in square brackets
            
            ep_patterns = [
                r'(?i)(?:^|\s)s\s*(\d{2})\s*e\s*(\d{2})(?=\W|$)',  # Case-insensitive SXXEXX format
                r'(?i)(?:^|\s)s?0*(\d{1,2})e0*(\d{1,3})(?=\W|$)',  # Case-insensitive SXXEXX format with leading zeros
                r'(?:^|\s)(?:ep|episode|e)[\s\.]*(\d{1,4})',        # For ep, episode, or e followed by the episode number
                r'(?:^|\s)- (\d{1,4})(?=\s|$|\.|_)',                  # Matches episode numbers like "- 15"
                r'(?:^|\s)\[(\d{1,4})\](?:\s|$|\.|_)',               # Matches numbers like "[15]"
                r'(?:^|\s)e(\d+)(?:\s|$|\.|_)',                      # Matches "e01" format
                r'(?<!\d)(\d{1,2})(?!\d)',                            # Fallback for any two-digit number
            ]

            episode = -1
            for pattern in ep_patterns:
                match = re.search(pattern, title_for_search)
                if match:
                    try:
                        # If we match SxEx, use the second group for episode number
                        if match.lastindex == 2:
                            ep = int(match.group(2))
                        else:
                            ep = int(match.group(1))
                        if ep < 5000:  # Ensures it is a valid episode number
                            episode = ep
                            break
                    except Exception as e:
                        print("🚨 Exception:", e)

            if is_movie and episode == -1:
                episode = 0  # If it's a movie, set episode to 0

            # Find magnet link
            magnet_el = row.select_one('td:nth-child(3) a[href^="magnet:"]')
            if magnet_el:
                results.append({
                    'title': title,
                    'episode': episode,
                    'magnet': magnet_el['href'],
                    'date': row.select_one('td:nth-child(5)').text.strip(),
                    'size': row.select_one('td:nth-child(4)').text.strip(),
                    'is_movie': is_movie
                })

        return results

    except requests.exceptions.RequestException as e:
        print(f"Error fetching data: {e}")
        return []

def add_torrent_to_qbittorrent(magnet_link):
    try:
        session = requests.Session()
        login_url = f"http://{QBITTORRENT_HOST}:{QBITTORRENT_PORT}/api/v2/auth/login"
        login_data = {"username": QBITTORRENT_USERNAME, "password": QBITTORRENT_PASSWORD}
        session.post(login_url, data=login_data)

        add_url = f"http://{QBITTORRENT_HOST}:{QBITTORRENT_PORT}/api/v2/torrents/add"
        res = session.post(add_url, data={"urls": magnet_link})

        return res.status_code == 200
    except Exception as e:
        print(f"[add_torrent] Error: {e}")
        return False

# ===============================
#  [5] Background: Pagination Detection
# ===============================
def detect_pagination(search_query):
    try:
        url = f"https://nyaa.si/?f=0&c=1_2&q={search_query}&p=1"
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'}
        response = requests.get(url, headers=headers)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # First try to get pagination data from the pagination list
        pagination = soup.select('ul.pagination li a')
        pages = []
        
        for page_link in pagination:
            try:
                page_num = int(page_link.text.strip())
                pages.append(page_num)
            except ValueError:
                # Skip non-numeric links (like "»" or "«")
                continue
                
        if pages:
            return {'total_pages': max(pages)}
        
        # If no pages found but has "next" link, assume at least 2 pages
        next_link = soup.select_one('ul.pagination li.next')
        if next_link and not next_link.get('class', []).count('disabled'):
            return {'total_pages': 2}
            
        # Default to 1 page if no pagination detected
        return {'total_pages': 1}
    except Exception as e:
        print(f"[detect_pagination] Error: {e}")
        return {'total_pages': 1}

# ===============================
#  [6] Full Download Logic
# ===============================
def download_all_episodes(anime_id, search_query):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    processed_episodes = set()
    page = 1
    latest_episode = 0

    while True:
        results = fetch_magnet_links(search_query, page)
        if not results:
            break
        results.sort(key=lambda x: x['episode'] if x['episode'] != -1 else 99999)

        for result in results:
            ep = result['episode']
            if ep in processed_episodes:
                continue
            processed_episodes.add(ep)
            if ep > latest_episode and ep != -1:
                latest_episode = ep

            if add_torrent_to_qbittorrent(result['magnet']):
                cursor.execute(
                    "INSERT INTO downloads (anime_id, episode, magnet_link, download_date) VALUES (?, ?, ?, ?)",
                    (anime_id, ep, result['magnet'], datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
                )
        page += 1

    if latest_episode > 0:
        cursor.execute("UPDATE anime SET last_episode = ? WHERE id = ?", (latest_episode, anime_id))

    conn.commit()
    conn.close()

def download_all_episodes_with_progress(anime_id, search_query, task_id):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # Get pagination info BEFORE starting the loop
    pagination_info = detect_pagination(search_query)
    total_pages = pagination_info['total_pages']
    
    # Update task with total pages immediately
    cursor.execute(
        "UPDATE tasks SET total_pages = ?, updated_at = ? WHERE id = ?",
        (total_pages, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), task_id)
    )
    conn.commit()
    
    page = 1
    processed_episodes = set()
    latest_episode = 0

    while page <= total_pages:
        results = fetch_magnet_links(search_query, page)
        results.sort(key=lambda x: x['episode'] if x['episode'] != -1 else 99999)

        for result in results:
            ep = result['episode']
            if ep in processed_episodes:
                continue
            processed_episodes.add(ep)
            if ep > latest_episode and ep != -1:
                latest_episode = ep
            if add_torrent_to_qbittorrent(result['magnet']):
                cursor.execute(
                    "INSERT INTO downloads (anime_id, episode, magnet_link, download_date) VALUES (?, ?, ?, ?)",
                    (anime_id, ep, result['magnet'], datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
                )
        progress = int((page / total_pages) * 100)
        cursor.execute(
            "UPDATE tasks SET current_page = ?, progress = ?, updated_at = ? WHERE id = ?",
            (page, progress, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), task_id)
        )
        conn.commit()
        page += 1

    if latest_episode > 0:
        cursor.execute("UPDATE anime SET last_episode = ? WHERE id = ?", (latest_episode, anime_id))

    cursor.execute(
        "UPDATE tasks SET status = 'completed', progress = 100, updated_at = ? WHERE id = ?",
        (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), task_id)
    )
    conn.commit()
    conn.close()

def check_new_episodes_with_progress(anime_id, search_query, start_episode, task_id):
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        
        # Get pagination info BEFORE starting the loop
        pagination_info = detect_pagination(search_query)
        total_pages = pagination_info['total_pages']
        
        # Update task with total pages immediately
        cursor.execute(
            "UPDATE tasks SET total_pages = ?, updated_at = ? WHERE id = ?",
            (total_pages, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), task_id)
        )
        conn.commit()
        
        page = 1
        processed_episodes = set()
        latest_episode = start_episode

        while page <= total_pages:
            results = fetch_magnet_links(search_query, page)
            results.sort(key=lambda x: x['episode'] if x['episode'] != -1 else 99999)

            for result in results:
                ep = result['episode']
                if ep <= start_episode or ep in processed_episodes or ep == -1:
                    continue
                    
                processed_episodes.add(ep)  # Mark this episode as processed
                if ep > latest_episode:
                    latest_episode = ep
                    
                if add_torrent_to_qbittorrent(result['magnet']):
                    cursor.execute(
                        "INSERT INTO downloads (anime_id, episode, magnet_link, download_date) VALUES (?, ?, ?, ?)",
                        (anime_id, ep, result['magnet'], datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
                    )
                    
            progress = int((page / total_pages) * 100)
            cursor.execute(
                "UPDATE tasks SET current_page = ?, progress = ?, updated_at = ? WHERE id = ?",
                (page, progress, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), task_id)
            )
            conn.commit()
            page += 1

        if latest_episode > start_episode:
            cursor.execute("UPDATE anime SET last_episode = ? WHERE id = ?", (latest_episode, anime_id))

        cursor.execute(
            "UPDATE tasks SET status = 'completed', progress = 100, updated_at = ? WHERE id = ?",
            (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), task_id)
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Error in check new episodes thread: {e}")
        # Mark task as failed in database
        try:
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE tasks SET status = 'failed', updated_at = ? WHERE id = ?",
                (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), task_id)
            )
            conn.commit()
            conn.close()
        except Exception as inner_e:
            print(f"Could not update task status: {inner_e}")
 
# ===============================
#  [6a] Userinput validation
# ===============================
def validate_input(text):
    """Sanitize user input for safer database operations"""
    if text is None:
        return ""
    # Remove potentially harmful characters, limit length
    sanitized = text.strip()
    # Limit to reasonable length
    return sanitized[:500]

# ===============================
#  [7] Flask Routes
# ===============================

@app.route('/')
def index():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM anime ORDER BY title")
    anime_list = cursor.fetchall()
    conn.close()
    return render_template('index.html', anime_list=anime_list)

@app.route('/add', methods=['GET', 'POST'])
def add_anime():
    if request.method == 'POST':
        title = validate_input(request.form['title'])
        search_query = validate_input(request.form['search_query'])
        
        if not title or not search_query:
            return "Title and search query cannot be empty", 400
            
        try:
            last_episode = max(0, int(request.form.get('last_episode', 0)))
        except ValueError:
            last_episode = 0  # Default to 0 if invalid input
        auto_download = 1 if 'auto_download' in request.form else 0
        download_all = 1 if 'download_all' in request.form else 0
        
        # Get schedule interval from dropdown
        schedule_interval = validate_input(request.form.get('schedule_interval', 'global'))
        
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO anime (title, search_query, last_episode, auto_download, schedule_interval) VALUES (?, ?, ?, ?, ?)",
            (title, search_query, last_episode, auto_download, schedule_interval)
        )
        anime_id = cursor.lastrowid
        conn.commit()

        # Pre-detect pagination for better initial display
        pagination_info = detect_pagination(search_query)
        total_pages = pagination_info['total_pages']

        if download_all:
            cursor.execute(
                "INSERT INTO tasks (anime_id, task_type, status, total_pages, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (anime_id, 'download_all', 'running', total_pages, 
                 datetime.now().strftime("%Y-%m-%d %H:%M:%S"), 
                 datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            )
            task_id = cursor.lastrowid
            conn.commit()
            t = threading.Thread(target=download_all_episodes_with_progress, args=(anime_id, search_query, task_id))
            t.daemon = True
            t.start()
            return redirect(url_for('download_status', anime_id=anime_id))
        elif last_episode > 0:
            # If a starting episode is specified but not downloading all, still check for new episodes immediately
            cursor.execute(
                "INSERT INTO tasks (anime_id, task_type, status, total_pages, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (anime_id, 'check_new', 'running', total_pages,
                 datetime.now().strftime("%Y-%m-%d %H:%M:%S"), 
                 datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            )
            task_id = cursor.lastrowid
            conn.commit()
            t = threading.Thread(target=check_new_episodes_with_progress, args=(anime_id, search_query, last_episode, task_id))
            t.daemon = True
            t.start()
            return redirect(url_for('download_status', anime_id=anime_id))
        conn.close()
        return redirect(url_for('index'))
    return render_template('add_anime.html')

@app.route('/edit/<int:anime_id>')
def edit_anime(anime_id):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM anime WHERE id = ?", (anime_id,))
    anime = cursor.fetchone()
    conn.close()
    return render_template('edit_anime.html', anime=anime)

@app.route('/update/<int:anime_id>', methods=['POST'])
def update_anime(anime_id):
    data = request.form
    
    # Validate input
    title = validate_input(data.get('title', ''))
    search_query = validate_input(data.get('search_query', ''))
    status = validate_input(data.get('status', 'watching'))
    schedule_interval = validate_input(data.get('schedule_interval', 'global'))
    
    if not title or not search_query:
        return "Title and search query cannot be empty", 400
    
    try:
        last_episode = max(0, int(data.get('last_episode', 0)))
    except ValueError:
        last_episode = 0  # Default to 0 if invalid input
        
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE anime SET title = ?, search_query = ?, status = ?, last_episode = ?, auto_download = ?, schedule_interval = ? WHERE id = ?",
        (title, search_query, status, 
         last_episode,
         1 if 'auto_download' in data else 0, 
         schedule_interval, 
         anime_id)
    )
    conn.commit()
    conn.close()
    return redirect(url_for('index'))

@app.route('/delete/<int:anime_id>')
def delete_anime(anime_id):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM anime WHERE id = ?", (anime_id,))
    cursor.execute("DELETE FROM downloads WHERE anime_id = ?", (anime_id,))
    conn.commit()
    conn.close()
    return redirect(url_for('index'))

@app.route('/search/<int:anime_id>')
def search_anime(anime_id):
    page = request.args.get('page', 1, type=int)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM anime WHERE id = ?", (anime_id,))
    anime = cursor.fetchone()
    conn.close()
    if not anime:
        return "Anime not found", 404
    results = fetch_magnet_links(anime['search_query'], page)
    return render_template('search_results.html', anime=anime, results=results, current_page=page)

@app.route('/downloads')
def view_downloads():
    page = request.args.get('page', 1, type=int)
    per_page = 10
    offset = (page - 1) * per_page

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    # Get total number of downloads for pagination
    cursor.execute("SELECT COUNT(*) FROM downloads")
    total_downloads = cursor.fetchone()[0]
    total_pages = (total_downloads + per_page - 1) // per_page

    # Fetch paginated results
    cursor.execute("""
        SELECT d.*, a.title as anime_title 
        FROM downloads d
        JOIN anime a ON d.anime_id = a.id
        ORDER BY d.download_date DESC
        LIMIT ? OFFSET ?
    """, (per_page, offset))
    downloads = cursor.fetchall()
    conn.close()

    return render_template(
        'downloads.html', 
        downloads=downloads, 
        current_page=page, 
        total_pages=total_pages
    )


@app.route('/download-status/<int:anime_id>')
def download_status(anime_id):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM anime WHERE id = ?", (anime_id,))
    anime = cursor.fetchone()
    cursor.execute("SELECT * FROM tasks WHERE anime_id = ? ORDER BY id DESC LIMIT 1", (anime_id,))
    task = cursor.fetchone()
    conn.close()
    return render_template('download_status.html', anime=anime, task=task)

@app.route('/downloads')
def view_downloads():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("""
        SELECT d.*, a.title as anime_title 
        FROM downloads d
        JOIN anime a ON d.anime_id = a.id
        ORDER BY d.download_date DESC
    """)
    downloads = cursor.fetchall()
    conn.close()
    return render_template('downloads.html', downloads=downloads)

@app.route('/task-status/<int:anime_id>')
def task_status(anime_id):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM tasks WHERE anime_id = ? ORDER BY id DESC LIMIT 1", (anime_id,))
    task = cursor.fetchone()
    conn.close()
    if task:
        return jsonify({
            'status': task['status'],
            'progress': task['progress'],
            'total_pages': task['total_pages'],
            'current_page': task['current_page']
        })
    return jsonify({'status': 'not_found'})

# ===============================
#  [8] Template Generator 
# ===============================
def create_templates():    
    print("Creating templates...")
    if not os.path.exists('templates'):
        print("Templates directory doesn't exist, creating it")
        os.makedirs('templates')
    else:
        print(f"Templates directory exists at {os.path.abspath('templates')}")
    
    # After creating a template file
    print(f"Created template file: {os.path.abspath('templates/index.html')}")
    # Rest of your function

    
    # Create base template
    with open('templates/base.html', 'w') as f:
        f.write('''<!DOCTYPE html>
<html>
<head>
    <title>Anime Watchlist</title>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0-alpha1/dist/css/bootstrap.min.css" rel="stylesheet">
    <style>
        body { padding-top: 20px; }
        .container { max-width: 960px; }
    </style>
</head>
<body>
    <div class="container">
        <header class="d-flex justify-content-between align-items-center mb-4 pb-3 border-bottom">
            <h1>Anime Watchlist</h1>
            <nav>
                <a href="/" class="btn btn-outline-primary me-2">Home</a>
                <a href="/add" class="btn btn-outline-success me-2">Add Anime</a>
                <a href="/downloads" class="btn btn-outline-info">Downloads</a>
            </nav>
        </header>
        
        <main>
            {% block content %}{% endblock %}
        </main>
        
        <footer class="mt-5 pt-3 text-muted border-top">
            2025 Anime Watchlist
        </footer>
    </div>
    
    <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0-alpha1/dist/js/bootstrap.bundle.min.js"></script>
    {% block scripts %}{% endblock %}
</body>
</html>''')
    
    # Create index template
    with open('templates/index.html', 'w') as f:
        f.write('''{% extends "base.html" %}
{% block content %}
    <h2>My Anime List</h2>
    
    {% if anime_list %}
        <div class="table-responsive">
            <table class="table table-striped table-hover">
                <thead>
                    <tr>
                        <th>Title</th>
                        <th>Status</th>
                        <th>Last Episode</th>
                        <th>Auto Download</th>
                        <th>Actions</th>
                    </tr>
                </thead>
                <tbody>
                    {% for anime in anime_list %}
                    <tr>
                        <td>{{ anime.title }}</td>
                        <td>{{ anime.status }}</td>
                        <td>{{ anime.last_episode }}</td>
                        <td>{{ "Yes" if anime.auto_download else "No" }}</td>
                        <td>
                            <a href="/edit/{{ anime.id }}" class="btn btn-sm btn-primary">Edit</a>
                            <a href="/search/{{ anime.id }}" class="btn btn-sm btn-info">Search</a>
                            <a href="/delete/{{ anime.id }}" class="btn btn-sm btn-danger" onclick="return confirm('Are you sure?')">Delete</a>
                        </td>
                    </tr>
                    {% endfor %}
                </tbody>
            </table>
        </div>
    {% else %}
        <div class="alert alert-info">
            No anime in your watchlist. <a href="/add">Add one now</a>!
        </div>
    {% endif %}
{% endblock %}''')
 
     # Create add_anime template with dropdown for schedule
    with open('templates/add_anime.html', 'w') as f:
        f.write('''{% extends "base.html" %}
{% block content %}
    <h2>Add New Anime</h2>
    
    <form method="post" class="mb-4">
        <div class="mb-3">
            <label for="title" class="form-label">Anime Title</label>
            <input type="text" class="form-control" id="title" name="title" required>
        </div>
        
        <div class="mb-3">
            <label for="search_query" class="form-label">Search Query</label>
            <input type="text" class="form-control" id="search_query" name="search_query" required>
            <div class="form-text">This will be used to search for episodes on Nyaa.si</div>
        </div>
        
        <div class="mb-3">
            <label for="last_episode" class="form-label">Start downloading at Episode</label>
            <input type="number" class="form-control" id="last_episode" name="last_episode" value="0" min="0">
            <div class="form-text">If you've already watched some episodes, enter the last episode number you've seen here. The system will only download episodes after this number.</div>
        </div>
        
        <div class="mb-3 form-check">
            <input type="checkbox" class="form-check-input" id="auto_download" name="auto_download" checked>
            <label class="form-check-label" for="auto_download">Enable auto-download for new episodes</label>
        </div>
        
        <div class="mb-3 form-check">
            <input type="checkbox" class="form-check-input" id="download_all" name="download_all">
            <label class="form-check-label" for="download_all">Download all existing episodes now</label>
        </div>
        
        <div class="mb-3">
            <label for="schedule_interval" class="form-label">Check Interval</label>
            <select class="form-select" id="schedule_interval" name="schedule_interval">
                <option value="global">Global Setting (Default)</option>
                <option value="15min">Every 15 minutes</option>
                <option value="30min">Every 30 minutes</option>
                <option value="45min">Every 45 minutes</option>
                <option value="1h">Every 1 hour</option>
                <option value="2h">Every 2 hours</option>
                <option value="5h">Every 5 hours</option>
            </select>
            <div class="form-text">How often to check for new episodes</div>
        </div>
        
        <button type="submit" class="btn btn-success">Add Anime</button>
        <a href="/" class="btn btn-secondary">Cancel</a>
    </form>
{% endblock %}''')
    
    
    # Update edit_anime template with dropdown for schedule
    with open('templates/edit_anime.html', 'w') as f:
        f.write('''{% extends "base.html" %}
{% block content %}
    <h2>Edit Anime</h2>
    
    <form method="post" action="/update/{{ anime.id }}" class="mb-4">
        <div class="mb-3">
            <label for="title" class="form-label">Anime Title</label>
            <input type="text" class="form-control" id="title" name="title" value="{{ anime.title }}" required>
        </div>
        
        <div class="mb-3">
            <label for="search_query" class="form-label">Search Query</label>
            <input type="text" class="form-control" id="search_query" name="search_query" value="{{ anime.search_query }}" required>
        </div>
        
        <div class="mb-3">
            <label for="last_episode" class="form-label">Last Episode Downloaded</label>
            <input type="number" class="form-control" id="last_episode" name="last_episode" value="{{ anime.last_episode }}" min="0">
            <div class="form-text">Set this to your current episode progress. The system will only download episodes after this number.</div>
        </div>
        
        <div class="mb-3">
            <label for="status" class="form-label">Status</label>
            <select class="form-select" id="status" name="status">
                <option value="watching" {% if anime.status == 'watching' %}selected{% endif %}>Watching</option>
                <option value="completed" {% if anime.status == 'completed' %}selected{% endif %}>Completed</option>
                <option value="on_hold" {% if anime.status == 'on_hold' %}selected{% endif %}>On Hold</option>
                <option value="dropped" {% if anime.status == 'dropped' %}selected{% endif %}>Dropped</option>
                <option value="plan_to_watch" {% if anime.status == 'plan_to_watch' %}selected{% endif %}>Plan to Watch</option>
            </select>
        </div>
        
        <div class="mb-3 form-check">
            <input type="checkbox" class="form-check-input" id="auto_download" name="auto_download" {% if anime.auto_download %}checked{% endif %}>
            <label class="form-check-label" for="auto_download">Enable auto-download for new episodes</label>
        </div>
        
        <div class="mb-3">
            <label for="schedule_interval" class="form-label">Check Interval</label>
            <select class="form-select" id="schedule_interval" name="schedule_interval">
                <option value="global" {% if anime.schedule_interval == 'global' %}selected{% endif %}>Global Setting (Default)</option>
                <option value="15min" {% if anime.schedule_interval == '15min' %}selected{% endif %}>Every 15 minutes</option>
                <option value="30min" {% if anime.schedule_interval == '30min' %}selected{% endif %}>Every 30 minutes</option>
                <option value="45min" {% if anime.schedule_interval == '45min' %}selected{% endif %}>Every 45 minutes</option>
                <option value="1h" {% if anime.schedule_interval == '1h' %}selected{% endif %}>Every 1 hour</option>
                <option value="2h" {% if anime.schedule_interval == '2h' %}selected{% endif %}>Every 2 hours</option>
                <option value="5h" {% if anime.schedule_interval == '5h' %}selected{% endif %}>Every 5 hours</option>
            </select>
            <div class="form-text">How often to check for new episodes</div>
        </div>
        
        <button type="submit" class="btn btn-primary">Update</button>
        <a href="/" class="btn btn-secondary">Cancel</a>
    </form>
{% endblock %}''')
    
    # Create search_results template
    with open('templates/search_results.html', 'w') as f:
        f.write('''{% extends "base.html" %}
{% block content %}
    <h2>Search Results for "{{ anime.title }}"</h2>
    
    <div class="mb-3">
        <a href="/" class="btn btn-secondary">Back to List</a>
        <a href="/search/{{ anime.id }}?page={{ current_page + 1 }}" class="btn btn-outline-primary">Next Page</a>
    </div>
    
    {% if results %}
        <div class="table-responsive">
            <table class="table table-striped">
                <thead>
                    <tr>
                        <th>Title</th>
                        <th>Episode</th>
                        <th>Size</th>
                        <th>Date</th>
                        <th>Action</th>
                    </tr>
                </thead>
                <tbody>
                    {% for result in results %}
                    <tr>
                        <td>{{ result.title }}</td>
                        <td>{{ result.episode if result.episode != -1 else "N/A" }}</td>
                        <td>{{ result.size }}</td>
                        <td>{{ result.date }}</td>
                        <td>
                            <button class="btn btn-sm btn-success download-btn" 
                                    data-magnet="{{ result.magnet }}"
                                    data-anime-id="{{ anime.id }}"
                                    data-episode="{{ result.episode }}">
                                Download
                            </button>
                        </td>
                    </tr>
                    {% endfor %}
                </tbody>
            </table>
        </div>
    {% else %}
        <div class="alert alert-warning">
            No results found. Try refining your search query.
        </div>
    {% endif %}
{% endblock %}

{% block scripts %}
<script>
document.addEventListener('DOMContentLoaded', function() {
    const downloadButtons = document.querySelectorAll('.download-btn');
    
    downloadButtons.forEach(button => {
        button.addEventListener('click', function() {
            const magnetLink = this.getAttribute('data-magnet');
            const animeId = this.getAttribute('data-anime-id');
            const episode = this.getAttribute('data-episode');
            
            // Button UI feedback
            const originalText = this.textContent;
            this.textContent = 'Adding...';
            this.disabled = true;
            
            // Send AJAX request
            fetch('/download', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify({
                    magnet: magnetLink,
                    anime_id: animeId,
                    episode: episode
                })
            })
            .then(response => response.json())
            .then(data => {
                if (data.success) {
                    this.textContent = 'Added!';
                    this.classList.remove('btn-success');
                    this.classList.add('btn-outline-success');
                } else {
                    this.textContent = 'Failed';
                    this.classList.remove('btn-success');
                    this.classList.add('btn-danger');
                }
            })
            .catch(error => {
                console.error('Error:', error);
                this.textContent = 'Error';
                this.classList.remove('btn-success');
                this.classList.add('btn-danger');
            });
        });
    });
});
</script>
{% endblock %}''')
    
    # Create download_status template
    with open('templates/download_status.html', 'w') as f:
        f.write('''{% extends "base.html" %}
{% block content %}
    <h2>Download Status for "{{ anime.title }}"</h2>
    
    <div class="mb-4">
        <a href="/" class="btn btn-secondary">Back to List</a>
    </div>
    
    <div class="card">
        <div class="card-body">
            <h5 class="card-title">Download Progress</h5>
            
            {% if task %}
                <div class="progress mb-3">
                    <div class="progress-bar progress-bar-striped progress-bar-animated" 
                         role="progressbar" 
                         style="width: {{ task.progress }}%;" 
                         aria-valuenow="{{ task.progress }}" 
                         aria-valuemin="0" 
                         aria-valuemax="100">
                        {{ task.progress }}%
                    </div>
                </div>
                
                <p>Status: <span id="status-text">{{ task.status }}</span></p>
                <p>Processing page <span id="current-page">{{ task.current_page }}</span> of <span id="total-pages">{{ task.total_pages }}</span></p>
                
                {% if task.status == 'running' %}
                <div class="alert alert-info">
                    This process is running in the background. You can leave this page and come back later.
                </div>
                {% elif task.status == 'completed' %}
                <div class="alert alert-success">
                    Download task completed successfully!
                </div>
                {% endif %}
            {% else %}
                <div class="alert alert-warning">
                    No active download task found for this anime.
                </div>
            {% endif %}
        </div>
    </div>
{% endblock %}

{% block scripts %}
{% if task and task.status == 'running' %}
<script>
document.addEventListener('DOMContentLoaded', function() {
    const statusText = document.getElementById('status-text');
    const currentPage = document.getElementById('current-page');
    const totalPages = document.getElementById('total-pages');
    const progressBar = document.querySelector('.progress-bar');
    
    function updateStatus() {
        fetch('/task-status/{{ anime.id }}')
            .then(response => response.json())
            .then(data => {
                if (data.status == 'not_found') {
                    clearInterval(interval);
                    return;
                }
                
                statusText.textContent = data.status;
                currentPage.textContent = data.current_page;
                totalPages.textContent = data.total_pages;
                progressBar.style.width = data.progress + '%';
                progressBar.textContent = data.progress + '%';
                progressBar.setAttribute('aria-valuenow', data.progress);
                
                if (data.status == 'completed') {
                    clearInterval(interval);
                    document.querySelector('.card-body').innerHTML += `
                        <div class="alert alert-success mt-3">
                            Download task completed successfully!
                        </div>
                    `;
                }
            })
            .catch(error => console.error('Error:', error));
    }
    
    const interval = setInterval(updateStatus, 5000);
});
</script>
{% endif %}
{% endblock %}''')
    
    # Create downloads template
    with open('templates/downloads.html', 'w') as f:
        f.write('''{% extends "base.html" %}
{% block content %}
<h2>Downloaded Episodes</h2>

{% if downloads %}
    <div class="table-responsive">
        <table class="table table-striped table-hover">
            <thead>
                <tr>
                    <th>Anime</th>
                    <th>Episode</th>
                    <th>Magnet Link</th>
                    <th>Download Date</th>
                </tr>
            </thead>
            <tbody>
                {% for d in downloads %}
                <tr>
                    <td>{{ d.anime_title }}</td>
                    <td>{{ d.episode }}</td>
                    <td><a href="{{ d.magnet_link }}" target="_blank">Magnet</a></td>
                    <td>{{ d.download_date }}</td>
                </tr>
                {% endfor %}
            </tbody>
        </table>
    </div>

    <!-- Pagination Controls -->
    <nav aria-label="Page navigation">
      <ul class="pagination justify-content-center">
        {% if current_page > 1 %}
        <li class="page-item">
          <a class="page-link" href="{{ url_for('view_downloads', page=current_page-1) }}">Previous</a>
        </li>
        {% else %}
        <li class="page-item disabled">
          <span class="page-link">Previous</span>
        </li>
        {% endif %}

        {% for page_num in range(1, total_pages + 1) %}
        <li class="page-item {% if page_num == current_page %}active{% endif %}">
          <a class="page-link" href="{{ url_for('view_downloads', page=page_num) }}">{{ page_num }}</a>
        </li>
        {% endfor %}

        {% if current_page < total_pages %}
        <li class="page-item">
          <a class="page-link" href="{{ url_for('view_downloads', page=current_page+1) }}">Next</a>
        </li>
        {% else %}
        <li class="page-item disabled">
          <span class="page-link">Next</span>
        </li>
        {% endif %}
      </ul>
    </nav>

{% else %}
    <div class="alert alert-info">No downloads found.</div>
{% endif %}
{% endblock %}''')

# ===============================
#  [9] Entrypoint
# ===============================
def check_for_new_episodes():
    print("[Scheduler] Checking for new episodes...")
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM anime WHERE auto_download = 1 AND status != 'completed'")
        anime_list = cursor.fetchall()

        for anime in anime_list:
            print(f"[AutoCheck] {anime['title']}")
            results = fetch_magnet_links(anime['search_query'])
            # Sort results by episode number to ensure we process in order
            results.sort(key=lambda x: x['episode'] if x['episode'] != -1 else 99999)
            
            # Track which episodes we've already processed
            processed_episodes = set()
            
            for r in results:
                if r['episode'] > anime['last_episode'] and r['episode'] != -1 and r['episode'] not in processed_episodes:
                    processed_episodes.add(r['episode'])  # Mark this episode as processed
                    
                    if add_torrent_to_qbittorrent(r['magnet']):
                        cursor.execute(
                            "INSERT INTO downloads (anime_id, episode, magnet_link, download_date) VALUES (?, ?, ?, ?)",
                            (anime['id'], r['episode'], r['magnet'], datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
                        )
                        cursor.execute(
                            "UPDATE anime SET last_episode = ? WHERE id = ? AND last_episode < ?",
                            (r['episode'], anime['id'], r['episode'])
                        )
                        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Error in scheduled check: {e}")

def run_scheduler():
    # Define the custom check function inside run_scheduler
    def schedule_custom_check():
        try:
            conn = sqlite3.connect(DB_PATH)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM anime WHERE auto_download = 1 AND status != 'completed' AND schedule_interval != 'global'")
            anime_list = cursor.fetchall()
            
            for anime in anime_list:
                print(f"[CustomScheduleCheck] {anime['title']}")
                results = fetch_magnet_links(anime['search_query'])
                processed_episodes = set()
                
                for r in sorted(results, key=lambda x: x['episode'] if x['episode'] != -1 else 99999):
                    if r['episode'] > anime['last_episode'] and r['episode'] != -1 and r['episode'] not in processed_episodes:
                        processed_episodes.add(r['episode'])
                        
                        if add_torrent_to_qbittorrent(r['magnet']):
                            cursor.execute(
                                "INSERT INTO downloads (anime_id, episode, magnet_link, download_date) VALUES (?, ?, ?, ?)",
                                (anime['id'], r['episode'], r['magnet'], datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
                            )
                            cursor.execute(
                                "UPDATE anime SET last_episode = ? WHERE id = ? AND last_episode < ?",
                                (r['episode'], anime['id'], r['episode'])
                            )
                            conn.commit()
            conn.close()
        except Exception as e:
            print(f"Error in custom schedule check: {e}")
    
    # Configure scheduler based on environment settings for global checks
    if SCHEDULE_UNIT == 'minute':
        schedule.every(SCHEDULE_INTERVAL).minutes.do(check_for_new_episodes)
    elif SCHEDULE_UNIT == 'hour':
        schedule.every(SCHEDULE_INTERVAL).hours.do(check_for_new_episodes)
    elif SCHEDULE_UNIT == 'day':
        schedule.every(SCHEDULE_INTERVAL).days.do(check_for_new_episodes)
    else:
        schedule.every(1).hour.do(check_for_new_episodes)  # Fallback to hourly
    
    # Schedule the custom per-anime check
    schedule.every(15).minutes.do(schedule_custom_check)
    
    while True:
        schedule.run_pending()
        time.sleep(60)  # Check every minute if there are pending schedules


if __name__ == '__main__':
    init_db()
    create_templates()
    
    # Create and properly start the scheduler thread
    scheduler_thread = threading.Thread(target=run_scheduler)
    scheduler_thread.daemon = True
    scheduler_thread.start()

    # Run initial check at startup
    check_for_new_episodes()
    
    port = int(os.environ.get('FLASK_PORT', 5000))
    app.run(debug=False, host='0.0.0.0', port=port)
