# Render Deployment Instructions

## Prerequisites
- GitHub repository with your code pushed
- Render account (https://render.com)

## Deployment Steps

### 1. Deploy Backend First
1. Go to Render Dashboard → "New +" → "Web Service"
2. Connect your GitHub repository
3. Render will auto-detect `render.yaml` and show both services
4. Deploy the **ai-lighting-api** service first
5. Wait for deployment to complete
6. Copy the backend URL (e.g., `https://ai-lighting-api.onrender.com`)

### 2. Update Frontend Configuration
1. In Render Dashboard, go to **ai-lighting-ui** service
2. Go to "Environment" tab
3. Update `VITE_API_URL` with your actual backend URL
4. Example: `https://ai-lighting-api.onrender.com`

### 3. Deploy Frontend
1. Trigger a manual deploy or push a new commit
2. The frontend will be available at your Render URL

### 4. Update render.yaml
After getting your backend URL, update the `VITE_API_URL` and routes in `render.yaml`:
```yaml
- key: VITE_API_URL
  value: "https://YOUR-ACTUAL-BACKEND-URL.onrender.com"
```

## Important Notes

### Data Persistence
- Render's free tier has **no persistent disk storage**
- Your `data/` folder will be reset on each deploy
- Consider using:
  - **Render Disks** (paid feature) for persistent storage
  - **Cloud storage** (AWS S3, Google Cloud Storage, etc.)
  - **Database** for structured data

### Environment Variables
Required environment variables are set in `render.yaml`:
- `PYTHON_VERSION`: 3.11
- `NODE_VERSION`: 18
- `API_HOST`: 0.0.0.0
- `VITE_API_URL`: Your backend URL

### File Uploads
Your app handles DWG file uploads. On Render free tier:
- Files will be lost on restart/redeploy
- Consider implementing cloud storage for production

### Health Checks
Backend has a `/health` endpoint configured for Render health checks.
Make sure it's implemented in your FastAPI app.

## Local Testing Before Deploy

### Test Backend:
```bash
cd /Users/macbook/Desktop/ai-lighting-project
pip install -r requirements.txt
uvicorn services.api.main:app --host 0.0.0.0 --port 8000
```

### Test Frontend:
```bash
cd ui
npm install
npm run build
npm run preview
```

## Troubleshooting

### Backend Issues:
- Check logs in Render Dashboard
- Verify all dependencies are in `requirements.txt`
- Ensure `uvicorn[standard]` is installed
- Check if data directories exist (created in config.py)

### Frontend Issues:
- Verify `VITE_API_URL` is correct
- Check browser console for CORS errors
- Ensure API routes are proxied correctly
- Test API calls directly to backend URL

### CORS Issues:
Your FastAPI already has CORS middleware configured for "*" which is good for testing.
For production, consider restricting to your frontend domain.

## Cost Considerations

**Free Tier Limitations:**
- Services spin down after 15 minutes of inactivity
- First request after spindown takes 30-90 seconds
- No persistent disk storage
- 750 hours/month free (for single service)

**Paid Options:**
- Persistent disks: ~$1/GB/month
- Always-on services: Starting at $7/month
- Custom domains: Free with any paid plan

## Next Steps After Deployment

1. Test all functionality on Render
2. Implement cloud storage for DWG files
3. Add proper database for job storage (currently in-memory)
4. Set up monitoring and logging
5. Configure custom domain (optional)
6. Restrict CORS to your actual domain
7. Add authentication/API keys if needed
