# Pre-Deployment Checklist for Render

## ✅ Files Created/Updated

- [x] `render.yaml` - Render service configuration
- [x] `runtime.txt` - Python version specification
- [x] `.python-version` - Python version file
- [x] `.gitignore` - Ignore unnecessary files
- [x] `ui/vite.config.js` - Updated for production API URL
- [x] `ui/.env.example` - Environment variable template
- [x] `.gitkeep` files - Preserve directory structure

## 📋 Before Deploying

### 1. Test Locally
- [ ] Backend runs: `uvicorn services.api.main:app --host 0.0.0.0 --port 8000`
- [ ] Frontend builds: `cd ui && npm run build`
- [ ] Health endpoint works: `curl http://localhost:8000/health`

### 2. Git Repository
- [ ] Code pushed to GitHub/GitLab/Bitbucket
- [ ] All changes committed
- [ ] Repository is public or Render has access

### 3. Configuration Review
- [ ] Check `requirements.txt` has all dependencies
- [ ] Check `ui/package.json` has all dependencies
- [ ] Verify Python version (3.11) is correct for your code
- [ ] Verify Node version (18) is correct

## 🚀 Deployment Steps

### Step 1: Connect Repository to Render
1. Sign up/login at https://render.com
2. Click "New +" → "Blueprint"
3. Connect your repository
4. Render will detect `render.yaml` automatically

### Step 2: Deploy Backend First
1. Deploy `ai-lighting-api` service
2. Wait for build to complete (5-10 minutes first time)
3. Copy the backend URL (e.g., `https://ai-lighting-api.onrender.com`)
4. Test health endpoint: `curl https://YOUR-BACKEND-URL/health`

### Step 3: Configure Frontend
1. Go to `ai-lighting-ui` service in Render Dashboard
2. Environment tab → Update `VITE_API_URL` to your backend URL
3. Trigger manual deploy or push a commit

### Step 4: Test Complete System
- [ ] Frontend loads successfully
- [ ] API calls work from frontend to backend
- [ ] File upload works (note: files won't persist on free tier)
- [ ] No CORS errors in browser console

## ⚠️ Important Considerations

### Data Persistence Issue
**Your current setup has a CRITICAL issue for production:**

The app stores files in local directories:
- `data/dwg/` - DWG uploads
- `data/exports/` - Generated exports
- `ml/models/` - ML models

**On Render's free tier, these will be DELETED on every deploy/restart!**

### Solutions:

#### Option 1: Add Render Disk (Recommended for production)
Add to `render.yaml` under the api service:
```yaml
disk:
  name: ai-lighting-data
  mountPath: /opt/render/project/src/data
  sizeGB: 10
```
Cost: ~$1/GB/month

#### Option 2: Use Cloud Storage
Modify code to use:
- AWS S3 / Google Cloud Storage for files
- Cloud database for job data (currently in-memory)

#### Option 3: Accept Data Loss (OK for testing)
- Files will be lost on restart
- Good enough for demos/testing
- Re-upload files as needed

### In-Memory Job Store
`services/api/main.py` uses in-memory dict for jobs:
```python
JOBS: dict[str, dict] = {}
```

**This means job history is lost on restart.**

For production, consider:
- Redis (Render offers Redis add-on)
- PostgreSQL for persistent job storage
- External database service

## 🔧 Troubleshooting

### Backend won't start
- Check build logs in Render Dashboard
- Verify all imports work
- Check if data directories are created (config.py does this)

### Frontend can't reach API
- Verify `VITE_API_URL` is set correctly
- Check CORS configuration in backend
- Look for errors in browser console
- Try API directly: `curl https://YOUR-API-URL/health`

### Slow first load
- Free tier spins down after 15 min inactivity
- First request takes 30-90 seconds to "wake up"
- Upgrade to paid tier for always-on service

## 💰 Cost Estimate

### Free Tier (both services)
- ✅ 750 hours/month per service
- ✅ Automatic SSL
- ❌ Spins down after 15 min idle
- ❌ No persistent disk
- ❌ Slower builds

### Paid Tier (recommended for production)
- Backend (Starter): $7/month
- Frontend (Static Site): Free
- Persistent Disk (10GB): ~$10/month
- **Total: ~$17/month**

## 📚 Next Steps After Successful Deployment

1. [ ] Set up custom domain (optional)
2. [ ] Implement persistent storage solution
3. [ ] Add authentication/API keys
4. [ ] Set up monitoring/logging
5. [ ] Implement proper error handling
6. [ ] Add rate limiting
7. [ ] Optimize CORS for production
8. [ ] Set up CI/CD for automatic deploys
9. [ ] Add backup strategy for data
10. [ ] Load testing

## 📖 Documentation

- Full instructions: See `RENDER_DEPLOY.md`
- Render docs: https://render.com/docs
- Questions? Check Render community forums

---

**Ready to deploy?** Start with Step 1 above! 🚀
