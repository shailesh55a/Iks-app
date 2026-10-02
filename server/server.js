import express from 'express';
import path from 'path';
import {fileURLToPath} from 'url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const app = express();
const PORT = Number(process.env.PORT || 3000);
const FRONTEND = path.join(__dirname, '..', 'frontend', 'dist');
const FASTAPI_URL = (process.env.FASTAPI_URL || 'http://127.0.0.1:8000').replace(/\/+$/, '');

app.disable('x-powered-by');
app.use(express.json({limit: '1mb'}));

app.use('/api', async (req, res) => {
  try {
    const target = `${FASTAPI_URL}${req.originalUrl}`;
    const headers = {};
    if (req.headers.authorization) headers.authorization = req.headers.authorization;
    if (!['GET','HEAD'].includes(req.method)) headers['content-type'] = req.headers['content-type'] || 'application/json';
    const upstream = await fetch(target, {
      method: req.method,
      headers,
      body: ['GET','HEAD'].includes(req.method) ? undefined : JSON.stringify(req.body ?? {}),
    });
    res.status(upstream.status);
    upstream.headers.forEach((value, key) => {
      if (!['transfer-encoding','connection','content-encoding'].includes(key.toLowerCase())) res.setHeader(key, value);
    });
    res.send(Buffer.from(await upstream.arrayBuffer()));
  } catch (error) {
    res.status(502).json({detail: 'FastAPI service unavailable', code: 'FASTAPI_UNAVAILABLE'});
  }
});

app.get('/node-health', (_req, res) => res.json({status: 'ok', service: 'IKSphere Node.js server'}));
app.use(express.static(FRONTEND, {extensions: ['html']}));
app.get('/{*splat}', (_req, res) => res.sendFile(path.join(FRONTEND, 'index.html')));

app.listen(PORT, () => console.log(`IKSphere Node server running on http://localhost:${PORT}`));
