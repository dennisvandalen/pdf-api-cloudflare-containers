import { Container } from "@cloudflare/containers";
import { Hono } from "hono";

// Durable Object class for the Python PDF API container
export class MyContainer extends Container<Env> {
  defaultPort = 8080;
  sleepAfter = "10m"; // Keep warm longer to avoid cold starts
}

const app = new Hono<{ Bindings: Env }>();

// Health/info
app.get("/", (c) => c.text("PDF API worker"));
app.get("/health", async (c) => {
  try {
    const container = c.env.PDF_API.get(
      c.env.PDF_API.idFromName("/pdf-api-singleton"),
    );
    const req = new Request(new URL("/health", "http://container"));
    return await container.fetch(req);
  } catch (err) {
    return c.json({ status: "starting", error: String(err) }, 503);
  }
});

// Proxy POST /generate to a singleton container instance
app.post("/generate", async (c) => {
  const container = c.env.PDF_API.get(
    c.env.PDF_API.idFromName("/pdf-api-singleton"),
  );
  const req = new Request(new URL("/generate", "http://container"), {
    method: "POST",
    headers: c.req.raw.headers,
    body: c.req.raw.body,
  });
  return await container.fetch(req);
});

// Proxy POST /flatten (and /flatten/upload)
app.post("/flatten", async (c) => {
  const container = c.env.PDF_API.get(
    c.env.PDF_API.idFromName("/pdf-api-singleton"),
  );
  const req = new Request(new URL("/flatten", "http://container"), {
    method: "POST",
    headers: c.req.raw.headers,
    body: c.req.raw.body,
  });
  return await container.fetch(req);
});

app.post("/flatten/upload", async (c) => {
  const container = c.env.PDF_API.get(
    c.env.PDF_API.idFromName("/pdf-api-singleton"),
  );
  const req = new Request(new URL("/flatten/upload", "http://container"), {
    method: "POST",
    headers: c.req.raw.headers,
    body: c.req.raw.body,
  });
  return await container.fetch(req);
});

// Proxy POST /page-dimensions (and /page-dimensions/upload)
app.post("/page-dimensions", async (c) => {
  const container = c.env.PDF_API.get(
    c.env.PDF_API.idFromName("/pdf-api-singleton"),
  );
  const req = new Request(new URL("/page-dimensions", "http://container"), {
    method: "POST",
    headers: c.req.raw.headers,
    body: c.req.raw.body,
  });
  return await container.fetch(req);
});

app.post("/page-dimensions/upload", async (c) => {
  const container = c.env.PDF_API.get(
    c.env.PDF_API.idFromName("/pdf-api-singleton"),
  );
  const req = new Request(new URL("/page-dimensions/upload", "http://container"), {
    method: "POST",
    headers: c.req.raw.headers,
    body: c.req.raw.body,
  });
  return await container.fetch(req);
});

// Proxy POST /crop-fit-mm (and /crop-fit-mm/upload)
app.post("/crop-fit-mm", async (c) => {
  const container = c.env.PDF_API.get(
    c.env.PDF_API.idFromName("/pdf-api-singleton"),
  );
  const req = new Request(new URL("/crop-fit-mm", "http://container"), {
    method: "POST",
    headers: c.req.raw.headers,
    body: c.req.raw.body,
  });
  return await container.fetch(req);
});

app.post("/crop-fit-mm/upload", async (c) => {
  const container = c.env.PDF_API.get(
    c.env.PDF_API.idFromName("/pdf-api-singleton"),
  );
  const req = new Request(new URL("/crop-fit-mm/upload", "http://container"), {
    method: "POST",
    headers: c.req.raw.headers,
    body: c.req.raw.body,
  });
  return await container.fetch(req);
});

// Proxy POST /overprint-preview (and /overprint-preview/upload)
app.post("/overprint-preview", async (c) => {
  const container = c.env.PDF_API.get(
    c.env.PDF_API.idFromName("/pdf-api-singleton"),
  );
  const req = new Request(new URL("/overprint-preview", "http://container"), {
    method: "POST",
    headers: c.req.raw.headers,
    body: c.req.raw.body,
  });
  return await container.fetch(req);
});

app.post("/overprint-preview/upload", async (c) => {
  const container = c.env.PDF_API.get(
    c.env.PDF_API.idFromName("/pdf-api-singleton"),
  );
  const req = new Request(new URL("/overprint-preview/upload", "http://container"), {
    method: "POST",
    headers: c.req.raw.headers,
    body: c.req.raw.body,
  });
  return await container.fetch(req);
});

// Proxy POST /render-cmyk-only (and /render-cmyk-only/upload)
app.post("/render-cmyk-only", async (c) => {
  const container = c.env.PDF_API.get(
    c.env.PDF_API.idFromName("/pdf-api-singleton"),
  );
  const req = new Request(new URL("/render-cmyk-only", "http://container"), {
    method: "POST",
    headers: c.req.raw.headers,
    body: c.req.raw.body,
  });
  return await container.fetch(req);
});

app.post("/render-cmyk-only/upload", async (c) => {
  const container = c.env.PDF_API.get(
    c.env.PDF_API.idFromName("/pdf-api-singleton"),
  );
  const req = new Request(new URL("/render-cmyk-only/upload", "http://container"), {
    method: "POST",
    headers: c.req.raw.headers,
    body: c.req.raw.body,
  });
  return await container.fetch(req);
});

// Proxy POST /render-spots-only (and /render-spots-only/upload)
app.post("/render-spots-only", async (c) => {
  const container = c.env.PDF_API.get(
    c.env.PDF_API.idFromName("/pdf-api-singleton"),
  );
  const req = new Request(new URL("/render-spots-only", "http://container"), {
    method: "POST",
    headers: c.req.raw.headers,
    body: c.req.raw.body,
  });
  return await container.fetch(req);
});

app.post("/render-spots-only/upload", async (c) => {
  const container = c.env.PDF_API.get(
    c.env.PDF_API.idFromName("/pdf-api-singleton"),
  );
  const req = new Request(new URL("/render-spots-only/upload", "http://container"), {
    method: "POST",
    headers: c.req.raw.headers,
    body: c.req.raw.body,
  });
  return await container.fetch(req);
});

// Proxy POST /quick-preview (and /quick-preview/upload)
app.post("/quick-preview", async (c) => {
  const container = c.env.PDF_API.get(
    c.env.PDF_API.idFromName("/pdf-api-singleton"),
  );
  const req = new Request(new URL("/quick-preview", "http://container"), {
    method: "POST",
    headers: c.req.raw.headers,
    body: c.req.raw.body,
  });
  return await container.fetch(req);
});

app.post("/quick-preview/upload", async (c) => {
  const container = c.env.PDF_API.get(
    c.env.PDF_API.idFromName("/pdf-api-singleton"),
  );
  const req = new Request(new URL("/quick-preview/upload", "http://container"), {
    method: "POST",
    headers: c.req.raw.headers,
    body: c.req.raw.body,
  });
  return await container.fetch(req);
});

// Proxy GET /optimize-starringyou/:id to container
app.get("/optimize-starringyou/:id", async (c) => {
  const container = c.env.PDF_API.get(
    c.env.PDF_API.idFromName("/pdf-api-singleton"),
  );
  const req = new Request(new URL(`/optimize-starringyou/${c.req.param("id")}`, "http://container"));
  return await container.fetch(req);
});

export default app;
