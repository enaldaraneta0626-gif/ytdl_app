from flask import Flask, request, jsonify, send_file, render_template, redirect, url_for, session, after_this_request
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from flask_bcrypt import Bcrypt
import os
import threading
import uuid
import re
import secrets
import shutil
import yt_dlp
import requests as http_requests
from pathlib import Path
from datetime import datetime, date
import tempfile

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev-secret-change-in-production')

database_url = os.environ.get('DATABASE_URL', 'sqlite:///ytdl.db')
if database_url.startswith('postgres://'):
    database_url = database_url.replace('postgres://', 'postgresql+pg8000://', 1)
if database_url.startswith('postgresql://') and 'pg8000' not in database_url:
    database_url = database_url.replace('postgresql://', 'postgresql+pg8000://', 1)
app.config['SQLALCHEMY_DATABASE_URI'] = database_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)
bcrypt = Bcrypt(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'

DOWNLOAD_DIR = Path(__file__).parent / "downloads"
DOWNLOAD_DIR.mkdir(exist_ok=True)

# ✏️ CHANGE YOUR AD VIDEO URL HERE ANYTIME
AD_YOUTUBE_URL = "https://www.youtube.com/watch?v=7tZAhO7BEnA"

GUEST_DAILY_LIMIT = 5
FREE_USER_DAILY_LIMIT = 10
WEBHOOK_URL = os.environ.get('WEBHOOK_URL', '')

YOUTUBE_PATTERN = re.compile(r'(youtube\.com|youtu\.be)')

# Optional cookies file to get past YouTube's "confirm you're not a bot"
# checks when running from a datacenter IP. Provide a Netscape-format
# cookies.txt exported from a logged-in browser. Either set
# YTDLP_COOKIES_FILE explicitly, or upload it as a Render Secret File named
# "cookies.txt" (auto-detected at /etc/secrets/cookies.txt).
_cookies_src = os.environ.get('YTDLP_COOKIES_FILE', '')
if not _cookies_src and os.path.exists('/etc/secrets/cookies.txt'):
    _cookies_src = '/etc/secrets/cookies.txt'

# yt-dlp rewrites the cookie file after each download, so it must live on a
# writable path. Render Secret Files are mounted read-only, so copy it into
# a writable location at startup and use that copy.
YTDLP_COOKIES_FILE = ''
if _cookies_src and os.path.exists(_cookies_src):
    try:
        writable_cookies = DOWNLOAD_DIR / 'cookies.txt'
        shutil.copyfile(_cookies_src, writable_cookies)
        YTDLP_COOKIES_FILE = str(writable_cookies)
    except Exception:
        YTDLP_COOKIES_FILE = _cookies_src

jobs = {}

class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(150), unique=True, nullable=False)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password = db.Column(db.String(200), nullable=False)
    is_premium = db.Column(db.Boolean, default=False)
    daily_download_count = db.Column(db.Integer, default=0)
    last_download_date = db.Column(db.Date, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    downloads = db.relationship('Download', backref='user', lazy=True)

    def reset_if_new_day(self):
        if self.last_download_date != date.today():
            self.daily_download_count = 0
            self.last_download_date = date.today()

class Download(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    title = db.Column(db.String(300))
    mode = db.Column(db.String(20))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

def get_guest_downloads_today():
    if session.get('guest_dl_date') != str(date.today()):
        session['guest_dl_date'] = str(date.today())
        session['guest_downloads'] = 0
    return session.get('guest_downloads', 0)

def can_download():
    if current_user.is_authenticated:
        if current_user.is_premium:
            return True, None
        current_user.reset_if_new_day()
        if current_user.daily_download_count >= FREE_USER_DAILY_LIMIT:
            return False, 'limit'
        return True, None
    else:
        if get_guest_downloads_today() >= GUEST_DAILY_LIMIT:
            return False, 'limit'
        return True, None

def downloads_remaining():
    if current_user.is_authenticated:
        if current_user.is_premium:
            return 999
        current_user.reset_if_new_day()
        return max(0, FREE_USER_DAILY_LIMIT - current_user.daily_download_count)
    return max(0, GUEST_DAILY_LIMIT - get_guest_downloads_today())

def fire_webhook(user):
    if not WEBHOOK_URL:
        return
    try:
        http_requests.post(WEBHOOK_URL, json={
            'username': user.username,
            'email': user.email,
            'signed_up_at': user.created_at.strftime('%Y-%m-%d %H:%M:%S'),
        }, timeout=5)
    except Exception:
        pass

def _progress_hook(job_id):
    """yt-dlp progress hook that mirrors download state into the jobs dict."""
    def hook(d):
        if d.get('status') == 'downloading':
            total = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
            downloaded = d.get('downloaded_bytes', 0)
            pct = int(downloaded / total * 90) if total else 0
            speed = d.get('speed')
            speed_str = f'{speed / 1024 / 1024:.1f} MB/s' if speed else ''
            jobs[job_id].update({
                'status': 'downloading',
                'percent': max(5, pct),
                'message': f'Downloading... {pct}%' if total else 'Downloading...',
                'speed': speed_str,
            })
        elif d.get('status') == 'finished':
            jobs[job_id].update({'status': 'downloading', 'percent': 95, 'message': 'Processing...'})
    return hook


def _base_ydl_opts():
    opts = {
        'quiet': True,
        'no_warnings': True,
        'noplaylist': True,
        # Try multiple player clients: helps both format availability and
        # bot-detection from datacenter IPs.
        'extractor_args': {'youtube': {'player_client': ['web', 'web_safari', 'mweb']}},
    }
    if YTDLP_COOKIES_FILE and os.path.exists(YTDLP_COOKIES_FILE):
        opts['cookiefile'] = YTDLP_COOKIES_FILE
    return opts


def ytdlp_download(url, mode, job_id, job_dir):
    """Download a video/audio stream with yt-dlp + ffmpeg."""
    jobs[job_id].update({'status': 'downloading', 'percent': 3, 'message': 'Fetching video info...'})

    ydl_opts = _base_ydl_opts()
    ydl_opts.update({
        'outtmpl': str(job_dir / '%(title)s.%(ext)s'),
        'progress_hooks': [_progress_hook(job_id)],
        'restrictfilenames': True,
    })

    if mode in ('mp3', 'convert'):
        ydl_opts.update({
            'format': 'bestaudio/best',
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            }],
        })
        ext = 'mp3'
    else:
        ydl_opts.update({
            'format': 'bestvideo+bestaudio/bestvideo/best',
            'merge_output_format': 'mp4',
        })
        ext = 'mp4'

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.extract_info(url, download=True)
    except yt_dlp.utils.DownloadError as e:
        msg = str(e)
        if 'Sign in to confirm' in msg or 'not a bot' in msg.lower():
            raise Exception("YouTube is blocking this server. An admin needs to set "
                            "YTDLP_COOKIES_FILE with a valid cookies.txt.")
        raise Exception("Download failed: " + msg.split('ERROR:')[-1].strip()[:200])

    # Locate the produced file (prefer the expected extension)
    files = sorted(job_dir.glob(f'*.{ext}')) or [p for p in job_dir.iterdir() if p.is_file()]
    if not files:
        raise Exception("Download produced no file.")
    filepath = files[0]
    return filepath, filepath.name

def download_worker(job_id, url, mode, user_id=None):
    job_dir = DOWNLOAD_DIR / job_id
    job_dir.mkdir(exist_ok=True)
    try:
        filepath, filename = ytdlp_download(url, mode, job_id, job_dir)
        title = filename.rsplit('.', 1)[0]

        jobs[job_id].update({
            'status': 'done', 'percent': 100, 'message': 'Done!',
            'filename': filename, 'filepath': str(filepath), 'title': title
        })

        with app.app_context():
            if user_id:
                user = User.query.get(user_id)
                if user:
                    user.reset_if_new_day()
                    user.daily_download_count += 1
                    user.last_download_date = date.today()
                    dl = Download(user_id=user_id, title=title, mode=mode)
                    db.session.add(dl)
                    db.session.commit()
    except Exception as e:
        jobs[job_id].update({'status': 'error', 'percent': 0, 'message': str(e)})

@app.route('/signup', methods=['GET', 'POST'])
def signup():
    if current_user.is_authenticated:
        return redirect(url_for('index'))
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        if not email or not username or not password:
            return render_template('signup.html', error='All fields are required.')
        if len(password) < 8:
            return render_template('signup.html', error='Password must be at least 8 characters.')
        if User.query.filter_by(email=email).first():
            return render_template('signup.html', error='Email already registered.')
        if User.query.filter_by(username=username).first():
            return render_template('signup.html', error='Username taken.')
        hashed = bcrypt.generate_password_hash(password).decode('utf-8')
        user = User(email=email, username=username, password=hashed)
        db.session.add(user)
        db.session.commit()
        fire_webhook(user)
        login_user(user)
        return redirect(url_for('index'))
    return render_template('signup.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('index'))
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        user = User.query.filter_by(email=email).first()
        if user and bcrypt.check_password_hash(user.password, password):
            login_user(user)
            return redirect(url_for('index'))
        return render_template('login.html', error='Invalid email or password.')
    return render_template('login.html')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('index'))

@app.route('/')
def index():
    dl_left = downloads_remaining()
    is_guest = not current_user.is_authenticated
    return render_template('index.html',
        downloads_left=dl_left,
        is_guest=is_guest,
        ad_url=AD_YOUTUBE_URL,
        guest_limit=GUEST_DAILY_LIMIT,
        free_limit=FREE_USER_DAILY_LIMIT)

@app.route('/dashboard')
@login_required
def dashboard():
    recent = Download.query.filter_by(user_id=current_user.id).order_by(Download.created_at.desc()).limit(20).all()
    dl_left = downloads_remaining()
    return render_template('dashboard.html', recent=recent, downloads_left=dl_left)

@app.route('/api/info', methods=['POST'])
def get_info():
    url = request.json.get('url', '').strip()
    if not YOUTUBE_PATTERN.search(url):
        return jsonify({'error': 'invalid_url', 'message': 'Only YouTube URLs are supported.'}), 400
    try:
        ydl_opts = _base_ydl_opts()
        ydl_opts['skip_download'] = True
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
        return jsonify({
            'title': info.get('title', 'YouTube Video'),
            'thumbnail': info.get('thumbnail'),
            'duration': info.get('duration'),
            'uploader': info.get('uploader') or info.get('channel'),
            'view_count': info.get('view_count'),
        })
    except Exception:
        return jsonify({'error': 'Could not fetch video info'}), 400

@app.route('/api/download', methods=['POST'])
def start_download():
    data = request.json
    url = data.get('url', '').strip()
    mode = data.get('mode', 'mp3')
    if not url:
        return jsonify({'error': 'No URL provided'}), 400
    if not YOUTUBE_PATTERN.search(url):
        return jsonify({'error': 'invalid_url', 'message': 'Only YouTube URLs are supported.'}), 400
    if mode not in ('mp4', 'mp3', 'convert'):
        return jsonify({'error': 'Invalid mode'}), 400
    allowed, reason = can_download()
    if not allowed:
        return jsonify({'error': 'limit', 'message': 'Daily limit reached!'}), 403
    is_guest = not current_user.is_authenticated
    job_id = str(uuid.uuid4())
    file_token = secrets.token_urlsafe(16)
    jobs[job_id] = {'status': 'starting', 'percent': 0, 'message': 'Starting...', 'guest': is_guest, 'guest_counted': False, 'file_token': file_token}
    user_id = current_user.id if current_user.is_authenticated else None
    thread = threading.Thread(target=download_worker, args=(job_id, url, mode, user_id))
    thread.daemon = True
    thread.start()
    show_ad = not current_user.is_authenticated
    return jsonify({'job_id': job_id, 'show_ad': show_ad, 'file_token': file_token})

@app.route('/api/status/<job_id>')
def job_status(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({'error': 'Job not found'}), 404
    if job.get('status') == 'done' and job.get('guest') and not job.get('guest_counted'):
        session['guest_downloads'] = get_guest_downloads_today() + 1
        session['guest_dl_date'] = str(date.today())
        jobs[job_id]['guest_counted'] = True
    return jsonify(job)

@app.route('/api/file/<job_id>')
def serve_file(job_id):
    job = jobs.get(job_id)
    if not job or job.get('status') != 'done':
        return jsonify({'error': 'File not ready'}), 404
    token = request.args.get('token', '')
    if token != job.get('file_token', ''):
        return jsonify({'error': 'Unauthorized'}), 403
    filepath = job.get('filepath')
    if not filepath or not os.path.exists(filepath):
        return jsonify({'error': 'File not found'}), 404

    job_dir = str(Path(filepath).parent)

    @after_this_request
    def cleanup(response):
        try:
            shutil.rmtree(job_dir, ignore_errors=True)
            jobs.pop(job_id, None)
        except Exception:
            pass
        return response

    return send_file(filepath, as_attachment=True, download_name=job.get('filename'))

with app.app_context():
    db.create_all()

if __name__ == '__main__':
    print("\n✅  YTDL running at: http://localhost:5100\n")
    app.run(host='0.0.0.0', port=5100, debug=False)
