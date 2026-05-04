# YTDL SaaS — YouTube Downloader with Auth

## Run Locally
```bash
pip install -r requirements.txt
python app.py
```
Open http://localhost:5100

---

## Deploy to Render (Free)

### Step 1 — Push to GitHub
1. Create a free account at https://github.com
2. Create a new repository called `ytdl`
3. Upload all these files to it

### Step 2 — Deploy on Render
1. Go to https://render.com and sign up free
2. Click **New → Web Service**
3. Connect your GitHub repo
4. Set these settings:
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `gunicorn app:app --workers 2 --timeout 120`
   - **Environment:** Python 3
5. Add these Environment Variables:
   - `SECRET_KEY` → any random string (e.g. `myrandomsecret123`)
6. Click **Deploy**

### Step 3 — Done!
Render gives you a free URL like `https://ytdl-xxxx.onrender.com`

---

## File Structure
```
ytdl_saas/
├── app.py              ← Flask app
├── requirements.txt    ← Dependencies
├── Procfile            ← Render start command
├── templates/
│   ├── index.html      ← Main page
│   ├── signup.html     ← Signup
│   ├── login.html      ← Login
│   └── dashboard.html  ← User dashboard
└── downloads/          ← Temp download folder
```

## Features
- 5 free downloads for guests
- Sign up to track downloads
- User dashboard with history
- Ready for Stripe payments (Phase 2)
