const GATEWAY = process.env.NEXT_PUBLIC_IPFS_GATEWAY ?? process.env.NEXT_PUBLIC_PINATA_GATEWAY;
export const FALLBACK_GATEWAY = "https://ipfs.io";

/** Gateway URL for a CID. NEXT_PUBLIC_IPFS_GATEWAY may be a full URL (e.g. the local dev API gateway). */
export const ipfsUrl = (cid: string, opts?: { w?: number }) => {
  const base = GATEWAY ? (GATEWAY.startsWith("http") ? GATEWAY : `https://${GATEWAY}`) : FALLBACK_GATEWAY;
  return `${base.replace(/\/$/, "")}/ipfs/${cid}${opts?.w ? `?img-width=${opts.w}` : ""}`;
};
