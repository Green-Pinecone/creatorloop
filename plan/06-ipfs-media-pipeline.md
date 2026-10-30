# Stage 06: IPFS Media Pipeline (Pinata)

**Goal:** creators upload images (video is a stretch) straight from the browser to IPFS without exposing the Pinata JWT. The app pins a validated metadata JSON and returns a CID that goes on-chain via `createPost`.

**Prerequisites:** Stage 01 (Pinata account, JWT, dedicated gateway). Can be built in parallel with Stages 02–05.

> After `createPost` confirms, the API indexer (Stage 04) picks up `PostCreated`, fetches this metadata JSON, caches `caption` and `media_sha256` in Postgres, and triggers the Moderator agent (Stage 09).
**Outputs:** Next.js route handlers `/api/upload-url` and `/api/metadata`, a client `uploadPost()` helper, a metadata schema, and gateway URL helpers.

---

## 1. Flow

```
Browser                                  Next.js server (route handlers)            Pinata
  │ 1. pick file, validate type/size       │                                         │
  │ 2. compress → webp ≤1600px             │                                         │
  │ 3. POST /api/upload-url  (Privy token)─►│ verify Privy token                     │
  │                                         │ create short-lived signed upload URL ──►│
  │ ◄─────────────── { url } ───────────────│                                         │
  │ 4. PUT/POST file to signed url ─────────┼────────────────────────────────────────►│ → mediaCid
  │ 5. POST /api/metadata {mediaCid, caption, dims, sha256} ─►│ validate, build JSON    │
  │                                         │ pin JSON ──────────────────────────────►│ → metadataCid
  │ ◄────────────── { metadataCid } ────────│                                         │
  │ 6. CreatorPosts.createPost(metadataCid) → chain                                   │
```

Why signed URLs: large files skip your server (no Vercel body-size limits), and the JWT never reaches the browser. The pattern is the same as ownsound, but safer, because ownsound piped uploads through its own Express backend.

## 2. Metadata schema (v1)

Pinned as JSON. The `metadataCid` is what goes on-chain.

```json
{
  "schema": "creatorloop.post.v1",
  "kind": "image",
  "media": {
    "cid": "bafybei...",
    "mime": "image/webp",
    "width": 1600,
    "height": 1200,
    "bytes": 248113,
    "sha256": "9f2c...e1"
  },
  "caption": "Sunset at Anjuna 🌅",
  "tags": ["goa", "sunset"],
  "createdAt": "2026-11-02T10:15:00Z",
  "author": "0xAbC...123"
}
```

Validation rules (enforced server-side in `/api/metadata`, mirrored in the client):

| Field | Rule |
|---|---|
| `schema` | Exactly `creatorloop.post.v1` |
| `kind` | `image` (v1); `video` later |
| `media.cid` | CIDv1 regex `^b[a-z2-7]{58,}$`; must resolve via a HEAD request to the gateway within 10 s |
| `media.mime` | `image/webp`, `image/jpeg`, `image/png` or `image/gif` |
| `media.bytes` | ≤ 10 MB |
| `caption` | ≤ 500 chars after trim; strip control chars; NFC normalize |
| `tags` | ≤ 5, each `^[a-z0-9-]{1,24}$` |
| `author` | Must equal the wallet tied to the verified Privy token |

`sha256` lets the Moderator detect duplicate reposts (Stage 09) and lets anyone confirm the media is unchanged.

Put the TypeScript type and a zod schema in `packages/shared/src/metadata.ts` so the web app and any Node scripts share one validator.

```ts
import { z } from "zod";
export const PostMetadataV1 = z.object({
  schema: z.literal("creatorloop.post.v1"),
  kind: z.literal("image"),
  media: z.object({
    cid: z.string().regex(/^b[a-z2-7]{58,}$/),
    mime: z.enum(["image/webp", "image/jpeg", "image/png", "image/gif"]),
    width: z.number().int().positive().max(8000),
    height: z.number().int().positive().max(8000),
    bytes: z.number().int().positive().max(10 * 1024 * 1024),
    sha256: z.string().regex(/^[0-9a-f]{64}$/),
  }),
  caption: z.string().max(500),
  tags: z.array(z.string().regex(/^[a-z0-9-]{1,24}$/)).max(5),
  createdAt: z.string().datetime(),
  author: z.string().regex(/^0x[0-9a-fA-F]{40}$/),
});
export type PostMetadataV1 = z.infer<typeof PostMetadataV1>;
```

## 3. Server: route handlers

Install `pnpm add pinata @privy-io/server-auth zod` in `apps/web`.

> **Verify SDK names:** Pinata's SDK (`pinata`, v2+) exposes upload helpers such as `pinata.upload.public.file(...)`, `pinata.upload.public.json(...)` and `pinata.upload.public.createSignedURL(...)`. Privy's server verification lives in `@privy-io/server-auth` (`PrivyClient#verifyAuthToken`) or its successor package. Check both READMEs for current method names before coding. The structure below stays the same regardless.

`apps/web/src/lib/pinata.ts`:

```ts
import "server-only";
import { PinataSDK } from "pinata";

export const pinata = new PinataSDK({
  pinataJwt: process.env.PINATA_JWT!,
  pinataGateway: process.env.NEXT_PUBLIC_PINATA_GATEWAY!,
});
```

`apps/web/src/lib/auth.ts`:

```ts
import "server-only";
import { PrivyClient } from "@privy-io/server-auth";
const privy = new PrivyClient(process.env.NEXT_PUBLIC_PRIVY_APP_ID!, process.env.PRIVY_APP_SECRET!);

/** Returns the Privy user's embedded wallet address, or throws. */
export async function requireWallet(req: Request): Promise<`0x${string}`> {
  const token = req.headers.get("authorization")?.replace(/^Bearer /, "");
  if (!token) throw new Response("unauthorized", { status: 401 });
  const claims = await privy.verifyAuthToken(token);
  const user = await privy.getUser(claims.userId);
  const wallet = user.wallet?.address;
  if (!wallet) throw new Response("no wallet", { status: 403 });
  return wallet as `0x${string}`;
}
```

`apps/web/src/app/api/upload-url/route.ts`:

```ts
import { pinata } from "@/lib/pinata";
import { requireWallet } from "@/lib/auth";
import { rateLimit } from "@/lib/rate-limit";

export async function POST(req: Request) {
  const wallet = await requireWallet(req);
  await rateLimit(`upload:${wallet}`, { max: 20, windowSec: 3600 });
  const url = await pinata.upload.public.createSignedURL({
    expires: 60,                           // seconds
    // restrict to images & size if the SDK supports it (mimeTypes / maxFileSize)
    name: `creatorloop-${wallet}-${Date.now()}`,
  });
  return Response.json({ url });
}
```

`apps/web/src/app/api/metadata/route.ts`:

```ts
import { pinata } from "@/lib/pinata";
import { requireWallet } from "@/lib/auth";
import { PostMetadataV1 } from "@creatorloop/shared/metadata";

export async function POST(req: Request) {
  const wallet = await requireWallet(req);
  const body = await req.json();
  const meta = PostMetadataV1.parse({
    ...body,
    schema: "creatorloop.post.v1",
    author: wallet,                                 // never trust client-provided author
    createdAt: new Date().toISOString(),
    caption: String(body.caption ?? "").normalize("NFC").replace(/[\u0000-\u001F\u007F]/g, "").trim(),
  });
  const head = await fetch(`https://${process.env.NEXT_PUBLIC_PINATA_GATEWAY}/ipfs/${meta.media.cid}`,
    { method: "HEAD", signal: AbortSignal.timeout(10_000) });
  if (!head.ok) return new Response("media not found", { status: 400 });

  const res = await pinata.upload.public.json(meta).name(`meta-${meta.media.cid}`);
  return Response.json({ metadataCid: res.cid });
}
```

Rate limiting: for the hackathon, an in-memory `Map` is fine on a single instance. On Vercel, use Upstash Redis or Vercel KV.

## 4. Client: `uploadPost()`

```ts
import imageCompression from "browser-image-compression";

export async function uploadPost(file: File, caption: string, tags: string[], getToken: () => Promise<string | null>) {
  if (!["image/jpeg", "image/png", "image/webp", "image/gif"].includes(file.type)) throw new Error("Unsupported type");
  if (file.size > 25 * 1024 * 1024) throw new Error("File too large");

  const media = file.type === "image/gif" ? file
    : await imageCompression(file, { maxWidthOrHeight: 1600, fileType: "image/webp", initialQuality: 0.85 });
  const { width, height } = await readDims(media);
  const sha256 = await sha256Hex(media);

  const token = await getToken();
  const auth = { Authorization: `Bearer ${token}` };

  const { url } = await (await fetch("/api/upload-url", { method: "POST", headers: auth })).json();
  const form = new FormData();
  form.append("file", media, `post.${media.type.split("/")[1]}`);
  form.append("network", "public");
  const up = await fetch(url, { method: "POST", body: form });
  if (!up.ok) throw new Error("Upload failed");
  const mediaCid: string = (await up.json()).data.cid;   // check response shape in Pinata docs

  const metaRes = await fetch("/api/metadata", {
    method: "POST",
    headers: { ...auth, "content-type": "application/json" },
    body: JSON.stringify({ kind: "image", caption, tags,
      media: { cid: mediaCid, mime: media.type, width, height, bytes: media.size, sha256 } }),
  });
  if (!metaRes.ok) throw new Error(await metaRes.text());
  return (await metaRes.json()).metadataCid as string;
}

async function sha256Hex(b: Blob) {
  const h = await crypto.subtle.digest("SHA-256", await b.arrayBuffer());
  return [...new Uint8Array(h)].map((x) => x.toString(16).padStart(2, "0")).join("");
}
async function readDims(b: Blob) {
  const bmp = await createImageBitmap(b);
  const d = { width: bmp.width, height: bmp.height };
  bmp.close();
  return d;
}
```

Privy's `getAccessToken()` from `usePrivy()` provides `getToken`.

## 5. Reading content

`packages/shared/src/ipfs.ts`:

```ts
const GATEWAY = process.env.NEXT_PUBLIC_PINATA_GATEWAY;
export const ipfsUrl = (cid: string, opts?: { w?: number }) =>
  `https://${GATEWAY}/ipfs/${cid}${opts?.w ? `?img-width=${opts.w}` : ""}`;
```

- Fetch metadata JSON with TanStack Query and a long `staleTime` (CIDs are immutable, so cache forever).
- Validate the fetched JSON with the same zod schema, and render a "broken post" card on failure. Never crash the feed.
- Use `next/image` with `remotePatterns` for the gateway domain, or plain `<img loading="lazy">` if image optimisation costs become an issue.
- Fallback gateway: if the dedicated gateway fails, retry via `https://ipfs.io/ipfs/<cid>`.

## 6. Moderation hooks (used in Stage 09)

- Keep every upload in a Pinata **group** (for example `creatorloop-amoy`) so you can list and unpin.
- When a post is hidden on-chain (`setHidden`), the API's moderation removal path (Stage 09) also **unpins** the media, so your gateway stops serving it. Other IPFS nodes may still have it; state that limitation.

## 7. Acceptance checklist

- [ ] The JWT never appears in the browser bundle (search `.next/static` for `PINATA`).
- [ ] An unauthenticated request to either route gets 401.
- [ ] Uploading a 12 MB PNG yields a WebP under 1 MB and a valid `metadataCid`.
- [ ] The metadata JSON from the gateway passes the zod schema, and `author` equals the logged-in wallet.
- [ ] Spoofing `author` in the request body has no effect.
- [ ] Captions with `<script>` render as text, not HTML.
- [ ] The rate limit triggers after 20 uploads per hour.

## 8. Pitfalls

- **Signed URL expiry:** 60 s is enough for an image. Raise it for video.
- **HEAD check right after upload:** gateways can take a few seconds to see a new CID. Retry 3× with backoff before failing.
- **EXIF GPS data:** WebP re-encoding strips EXIF, which is a privacy win. Keep it on for JPEGs (don't upload originals).
- **GIF compression** is skipped. Enforce a 5 MB cap on GIFs specifically.
