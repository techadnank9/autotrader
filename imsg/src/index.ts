/**
 * iMessage relay for AI Trader.
 *
 * The Python app owns every decision. This service only moves messages:
 *   POST /imsg/send     Python -> iMessage (text, optional poll), internal secret required
 *   POST /imsg/webhook  Photon Spectrum -> Python /api/imessage/inbound, then sends its reply
 */
import { Hono } from "hono";
import { waitUntil } from "@vercel/functions";
import { Spectrum, poll } from "spectrum-ts";
import { imessage } from "spectrum-ts/providers/imessage";

const INTERNAL = process.env.IMSG_INTERNAL_SECRET || "";
const BASE = (process.env.APP_BASE_URL || "").replace(/\/$/, "");

let spectrum: Promise<any> | null = null;
function client() {
  spectrum ??= Spectrum({
    projectId: process.env.SPECTRUM_PROJECT_ID!,
    projectSecret: process.env.SPECTRUM_PROJECT_SECRET!,
    providers: [imessage.config()],
    webhookSecret: process.env.SPECTRUM_WEBHOOK_SECRET,
  }).catch((err: unknown) => { spectrum = null; throw err; });
  return spectrum;
}

type Out = { text?: string; poll?: { title: string; options: string[] } };

async function deliver(space: any, out: Out) {
  if (out.text) await space.send(out.text);
  if (out.poll && out.poll.options.length >= 2) await space.send(poll(out.poll.title, out.poll.options));
}

const app = new Hono();

app.get("/imsg/health", (c) => c.json({ ok: true }));

app.post("/imsg/send", async (c) => {
  if (!INTERNAL || c.req.header("x-internal-secret") !== INTERNAL) return c.json({ ok: false }, 401);
  const body = await c.req.json<{ phone: string } & Out>();
  try {
    const im = imessage(await client());
    const space = await im.space.create(await im.user(body.phone));
    await deliver(space, body);
    return c.json({ ok: true });
  } catch (err) {
    console.error("send failed", err);
    return c.json({ ok: false, error: String((err as Error)?.message || err).slice(0, 300) }, 502);
  }
});

app.post("/imsg/webhook", async (c) => {
  const app_ = await client();
  // The SDK runs the handler after it builds the response; keep this invocation alive until it finishes.
  let finish!: () => void;
  const done = new Promise<void>((r) => (finish = r));
  const res: Response = await app_.webhook(c.req.raw, async (space: any, message: any) => {
    try {
      if (message.direction !== "inbound") return;
      const ct = message.content || {};
      const payload = {
        message_id: message.id,
        phone: message.sender?.id || "",
        text: ct.type === "text" ? ct.text : null,
        vote: ct.type === "poll_option" && ct.selected !== false ? (ct.option?.title || ct.title || null) : null,
      };
      if (!payload.text && !payload.vote) return;
      const r = await fetch(`${BASE}/api/imessage/inbound`, {
        method: "POST",
        headers: { "content-type": "application/json", "x-internal-secret": INTERNAL },
        body: JSON.stringify(payload),
      });
      const out = (await r.json().catch(() => ({}))) as Out;
      await deliver(space, out);
    } catch (err) {
      console.error("inbound failed", err);
    } finally {
      finish();
    }
  });
  waitUntil(Promise.race([done, new Promise((r) => setTimeout(r, 55_000))]));
  return res;
});

export default app;
