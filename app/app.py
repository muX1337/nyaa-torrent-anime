# ===============================
#  [1] Imports
# ===============================
import os
import time
import json
import re
import threading
import sqlite3
from datetime import datetime

import requests
from requests.adapters import HTTPAdapter, Retry
from bs4 import BeautifulSoup
from flask import Flask, render_template, request, jsonify, redirect, url_for
import schedule

# ===============================
#  [2] Environment Config
# ===============================
app = Flask(__name__)

QBITTORRENT_HOST = os.environ.get('QBITTORRENT_HOST', 'localhost')
QBITTORRENT_PORT = os.environ.get('QBITTORRENT_PORT', '8880')
QBITTORRENT_USERNAME = os.environ.get('QBITTORRENT_USERNAME', 'admin')
QBITTORRENT_PASSWORD = os.environ.get('QBITTORRENT_PASSWORD', 'adminadmin')
DB_PATH = os.environ.get('DB_PATH', 'data/anime_watchlist.db')

SCHEDULE_INTERVAL = int(os.environ.get('SCHEDULE_INTERVAL', 1))  # Default: every 1 hour
SCHEDULE_UNIT = os.environ.get('SCHEDULE_UNIT', 'hour')          # 'minute', 'hour', 'day'

# ===============================
#  [2a] HTTP session with retries/timeouts
# ===============================
REQUEST_TIMEOUT = (5, 20)  # (connect, read) seconds

def make_session():
    s = requests.Session()
    retries = Retry(
        total=5,
        connect=5,
        read=5,
        backoff_factor=0.5,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["HEAD", "GET", "OPTIONS", "POST"]
    )
    adapter = HTTPAdapter(max_retries=retries)
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
    })
    return s

http = make_session()

# ===============================
#  [3] Database Setup
# ===============================
def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS anime (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            search_query TEXT NOT NULL,
            status TEXT DEFAULT 'watching',
            last_episode INTEGER DEFAULT 0,
            next_episode_date TEXT,
            auto_download BOOLEAN DEFAULT 1,
            schedule_interval TEXT DEFAULT 'global'
        )
    ''')

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
#  [3a] Init on import
# ===============================
    
def _start_scheduler_once():
    """Start the scheduler exactly once across all gunicorn workers using a lockfile."""
    lock_path = "/tmp/anime_scheduler.lock"
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        try:
            os.write(fd, str(os.getpid()).encode())
        finally:
            os.close(fd)
        # winner: start scheduler thread
        t = threading.Thread(target=run_scheduler, daemon=True)
        t.start()
        print("[Scheduler] started in PID", os.getpid())
    except FileExistsError:
        # another worker already started it
        pass

# run-on-import (safe/idempotent)
try:
    init_db()
except Exception as e:
    print("init_db failed:", e)

try:
    _start_scheduler_once()
except Exception as e:
    print("scheduler start failed:", e)

# ===============================
#  [4] Magnet Fetching & Torrent Add
# ===============================
def fetch_magnet_links(search_query, page=1):
    try:
        url = f"https://nyaa.si/?f=0&c=1_2&q={search_query}&p={page}"
        resp = http.get(url, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, 'html.parser')

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
            title_for_search = re.sub(r'\[\w{8}\]', '', title_lower)

            ep_patterns = [
                r'(?i)(?:^|\s)s\s*(\d{2})\s*e\s*(\d{2})(?=\W|$)',
                r'(?i)(?:^|\s)s?0*(\d{1,2})e0*(\d{1,3})(?=\W|$)',
                r'(?:^|\s)(?:ep|episode|e)[\s\.]*(\d{1,4})',
                r'(?:^|\s)- (\d{1,4})(?=\s|$|\.|_)',
                r'(?:^|\s)\[(\d{1,4})\](?:\s|$|\.|_)',
                r'(?:^|\s)e(\d+)(?:\s|$|\.|_)',
                r'(?<!\d)(\d{1,2})(?!\d)',
            ]

            episode = -1
            for pattern in ep_patterns:
                match = re.search(pattern, title_for_search)
                if match:
                    try:
                        ep = int(match.group(2)) if (match.lastindex == 2) else int(match.group(1))
                        if ep < 5000:
                            episode = ep
                            break
                    except Exception as e:
                        print("Episode parse exception:", e)

            if is_movie and episode == -1:
                episode = 0

            magnet_el = row.select_one('td:nth-child(3) a[href^="magnet:"]')
            if magnet_el:
                results.append({
                    'title': title,
                    'episode': episode,
                    'magnet': magnet_el['href'],
                    'date': row.select_one('td:nth-child(5)').text.strip() if row.select_one('td:nth-child(5)') else '',
                    'size': row.select_one('td:nth-child(4)').text.strip() if row.select_one('td:nth-child(4)') else '',
                    'is_movie': is_movie
                })

        return results

    except requests.exceptions.RequestException as e:
        print(f"Error fetching data: {e}")
        return []

def add_torrent_to_qbittorrent(magnet_link):
    try:
        s = make_session()
        login_url = f"http://{QBITTORRENT_HOST}:{QBITTORRENT_PORT}/api/v2/auth/login"
        login_data = {"username": QBITTORRENT_USERNAME, "password": QBITTORRENT_PASSWORD}
        s.post(login_url, data=login_data, timeout=REQUEST_TIMEOUT)

        add_url = f"http://{QBITTORRENT_HOST}:{QBITTORRENT_PORT}/api/v2/torrents/add"
        res = s.post(add_url, data={"urls": magnet_link}, timeout=REQUEST_TIMEOUT)
        return res.status_code == 200
    except Exception as e:
        print(f"[add_torrent] Error: {e}")
        return False

# ===============================
#  [5] Pagination Detection
# ===============================
def detect_pagination(search_query):
    try:
        url = f"https://nyaa.si/?f=0&c=1_2&q={search_query}&p=1"
        resp = http.get(url, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, 'html.parser')

        pagination = soup.select('ul.pagination li a')
        pages = []
        for page_link in pagination:
            try:
                page_num = int(page_link.text.strip())
                pages.append(page_num)
            except ValueError:
                continue

        if pages:
            return {'total_pages': max(pages)}

        next_link = soup.select_one('ul.pagination li.next')
        if next_link and not next_link.get('class', []).count('disabled'):
            return {'total_pages': 2}

        return {'total_pages': 1}
    except Exception as e:
        print(f"[detect_pagination] Error: {e}")
        return {'total_pages': 1}

# ===============================
#  [6] Download logic (unchanged)
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

    pagination_info = detect_pagination(search_query)
    total_pages = pagination_info['total_pages']
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

        pagination_info = detect_pagination(search_query)
        total_pages = pagination_info['total_pages']
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
                processed_episodes.add(ep)
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
    if text is None:
        return ""
    return text.strip()[:500]

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
            last_episode = 0
        auto_download = 1 if 'auto_download' in request.form else 0
        download_all = 1 if 'download_all' in request.form else 0
        schedule_interval = validate_input(request.form.get('schedule_interval', 'global'))

        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO anime (title, search_query, last_episode, auto_download, schedule_interval) VALUES (?, ?, ?, ?, ?)",
            (title, search_query, last_episode, auto_download, schedule_interval)
        )
        anime_id = cursor.lastrowid
        conn.commit()

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
            t = threading.Thread(target=download_all_episodes_with_progress, args=(anime_id, search_query, task_id), daemon=True)
            t.start()
            return redirect(url_for('download_status', anime_id=anime_id))
        elif last_episode > 0:
            cursor.execute(
                "INSERT INTO tasks (anime_id, task_type, status, total_pages, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (anime_id, 'check_new', 'running', total_pages,
                 datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                 datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            )
            task_id = cursor.lastrowid
            conn.commit()
            t = threading.Thread(target=check_new_episodes_with_progress, args=(anime_id, search_query, last_episode, task_id), daemon=True)
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

    title = validate_input(data.get('title', ''))
    search_query = validate_input(data.get('search_query', ''))
    status = validate_input(data.get('status', 'watching'))
    schedule_interval = validate_input(data.get('schedule_interval', 'global'))

    if not title or not search_query:
        return "Title and search query cannot be empty", 400

    try:
        last_episode = max(0, int(data.get('last_episode', 0)))
    except ValueError:
        last_episode = 0

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE anime SET title = ?, search_query = ?, status = ?, last_episode = ?, auto_download = ?, schedule_interval = ? WHERE id = ?",
        (title, search_query, status, last_episode, 1 if 'auto_download' in data else 0, schedule_interval, anime_id)
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
    # (keep the paginated version)
    page = request.args.get('page', 1, type=int)
    per_page = 10
    offset = (page - 1) * per_page

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    cursor.execute("SELECT COUNT(*) FROM downloads")
    total_downloads = cursor.fetchone()[0]
    total_pages = (total_downloads + per_page - 1) // per_page

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
#  [8] Scheduler
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
            results.sort(key=lambda x: x['episode'] if x['episode'] != -1 else 99999)

            processed_episodes = set()
            for r in results:
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
        print(f"Error in scheduled check: {e}")
        
def run_scheduler():
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

    # global schedule
    if SCHEDULE_UNIT == 'minute':
        schedule.every(SCHEDULE_INTERVAL).minutes.do(check_for_new_episodes)
    elif SCHEDULE_UNIT == 'hour':
        schedule.every(SCHEDULE_INTERVAL).hours.do(check_for_new_episodes)
    elif SCHEDULE_UNIT == 'day':
        schedule.every(SCHEDULE_INTERVAL).days.do(check_for_new_episodes)
    else:
        schedule.every(1).hour.do(check_for_new_episodes)

    # per-anime 15-min schedule
    schedule.every(15).minutes.do(schedule_custom_check)

    # run loop
    while True:
        try:
            schedule.run_pending()
        except Exception as e:
            print("Scheduler loop error:", e)
        time.sleep(60)

# ===============================
#  Entrypoint (dev-run only)
# ===============================
if __name__ == '__main__':
    init_db()

    scheduler_thread = threading.Thread(target=run_scheduler, daemon=True)
    scheduler_thread.start()

    # dev server; in Docker we use gunicorn
    port = int(os.environ.get('FLASK_PORT', 5000))
    app.run(debug=False, host='0.0.0.0', port=port, threaded=True)
