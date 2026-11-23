// POST   /api/publish?block=B3   body: .3dm bytes, header x-workshop-key  -> publish a block
// POST   /api/publish?block=B3&upload=<id>&part=<i>&parts=<n>&size=<bytes>
//                                 body: one part (4 MB; the last one less)  -> a model too big for
//                                 one request, in parts (Vercel Functions take 4.5 MB at most); the
//                                 last part publishes it
// DELETE /api/publish?block=B3   header x-workshop-key                    -> remove one block
// DELETE /api/publish?block=ALL  header x-workshop-key                    -> clear everything
import * as blob from '@vercel/blob';
import { checkKey, json, looksLike3dm, MAX_BYTES, normaliseBlock, PART_BYTES, publishBlock, receivePart, removeBlock } from '../lib/manifest.js';

export default {
  async fetch(request) {
    if (request.method === 'OPTIONS') {
      return new Response(null, {
        headers: {
          'access-control-allow-origin': '*',
          'access-control-allow-methods': 'POST, DELETE, OPTIONS',
          'access-control-allow-headers': 'content-type, x-workshop-key',
        },
      });
    }
    if (request.method !== 'POST' && request.method !== 'DELETE') {
      return json({ ok: false, error: 'Use POST to publish or DELETE to remove.' }, 405);
    }

    const keyError = checkKey(request);
    if (keyError) return json({ ok: false, error: keyError }, 401);

    const url = new URL(request.url);
    const block = normaliseBlock(url.searchParams.get('block'));
    if (!block) return json({ ok: false, error: 'Missing or invalid ?block= (letters, digits, - and _ only).' }, 400);

    try {
      if (request.method === 'DELETE') {
        return json({ ok: true, block, ...(await removeBlock(blob, block)) });
      }

      if (block === 'ALL') return json({ ok: false, error: '"ALL" is reserved for clearing.' }, 400);
      const bytes = new Uint8Array(await request.arrayBuffer());
      if (bytes.length === 0) return json({ ok: false, error: 'Empty upload.' }, 400);
      if (url.searchParams.has('upload')) {
        const n = (k) => Number(url.searchParams.get(k));
        const meta = { upload: url.searchParams.get('upload'), part: n('part'), parts: n('parts'), size: n('size') };
        return json({ ok: true, block, ...(await receivePart(blob, block, meta, bytes)) });
      }
      if (bytes.length > PART_BYTES) {   // in practice Vercel turns these away before they get here
        return json({ ok: false, error: `Models over ${PART_BYTES / 1048576} MB go up in parts (up to ${MAX_BYTES / 1048576} MB): update the Rhino or Grasshopper script.` }, 413);
      }
      if (!looksLike3dm(bytes)) return json({ ok: false, error: 'That is not a Rhino .3dm file.' }, 400);

      return json({ ok: true, ...(await publishBlock(blob, block, bytes)) });
    } catch (err) {
      if (!err.status) console.error(err);
      return json({ ok: false, error: String((err && err.message) || err) }, err.status || 500);
    }
  },
};
