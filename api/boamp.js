// Proxy serverless Vercel — relaie les appels à l'API BOAMP pour contourner
// le blocage CORS rencontré depuis le navigateur. Le site (index.html) appelle
// /api/boamp au lieu d'appeler directement boamp-datadila.opendatasoft.com.
//
// Utilise le module natif https plutôt que fetch, pour éviter toute
// dépendance à une version de Node.js où fetch ne serait pas disponible.

const https = require('https');

module.exports = (request, response) => {
  const query = request.query || {};
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) {
    params.set(key, Array.isArray(value) ? value[0] : value);
  }

  const path = `/api/explore/v2.1/catalog/datasets/boamp/records?${params.toString()}`;

  const upstreamReq = https.request(
    {
      hostname: 'boamp-datadila.opendatasoft.com',
      path,
      method: 'GET',
      headers: { 'Accept': 'application/json' },
    },
    (upstreamRes) => {
      let body = '';
      upstreamRes.on('data', (chunk) => { body += chunk; });
      upstreamRes.on('end', () => {
        response.setHeader('Access-Control-Allow-Origin', '*');
        response.setHeader('Cache-Control', 's-maxage=300, stale-while-revalidate');
        response.status(upstreamRes.statusCode || 502).send(body);
      });
    }
  );

  upstreamReq.on('error', (err) => {
    response.status(502).json({ error: "Échec de la requête vers l'API BOAMP", detail: String(err) });
  });

  upstreamReq.end();
};
