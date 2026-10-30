import { z } from "zod";

// Mirror of services/api/app/metadata.py. Keep the two in sync.
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
