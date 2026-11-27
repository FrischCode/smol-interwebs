/**
 * Offline Search — pi reference adapter for SIW.
 *
 * This file is the reference, not the product. SIW is a server; the harness writes this
 * tool against its documented API (`GET /spec` on the server; web/CONTRACT.md in the tree). Read it
 * the way you'd read the contract it consumes: the SIW server owns the corpus, the pick,
 * the lifecycle and the ranking — this file only knows the HTTP contract and how to
 * render it. It contains no kiwix-serve logic: no spawning, no self-heal, no corpus
 * discovery, no feed parsing. Those are the server's acts.
 *
 *   SIW_URL — the API base, default "http://127.0.0.1:7662" (the config's api_port).
 *
 * To use: copy this file into your pi extension folder (or import it), set SIW_URL,
 * restart the harness, and start SIW once with `siw serve`. A run that then
 * searches offline is just this tool called — no other wiring.
 *
 * The split the tool keeps is the same one the contract keeps: the model picks the
 * words and the narrowing; the endpoint travels as a boot fact (the env var), exactly
 * the way the shipped extensions split the words from the address.
 */
import { Type } from "typebox";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

const SIW_URL = process.env.SIW_URL || "http://127.0.0.1:7662";

export default function siwOfflineSearch(pi: ExtensionAPI) {
  pi.registerTool({
    name: "offline_search",
    label: "Offline Search",
    description:
      "Offline fulltext search over the machine's kiwix corpus (no internet). " +
      "Returns ranked results: title, book, URL, snippet. " +
      "The server owns the corpus — this tool searches only what the operator configured. " +
      "Zero-hit semantics: a 0-hit on a scoped route means NOT IN THESE SOURCES, not 'not in corpus'; " +
      "rerun on the global route and/or loosen the query (search is AND across all terms) — only a global " +
      "0-hit is a corpus-level conclusion.",
    parameters: Type.Object({
      query: Type.String({ description: "The words to search. The index ANDs all terms." }),
      limit: Type.Optional(
        Type.Number({ description: "Maximum results to return. Default: 5, max: 25." }),
      ),
      sources: Type.Optional(
        Type.String({
          description:
            "Narrowing by book name: comma/space-separated substrings (the exact names are at `GET /catalog`). " +
            "Only matching books are searched.",
        }),
      ),
      corpus: Type.Optional(
        Type.String({
          description: "Narrowing by family substring, e.g. 'wikipedia'. Empty = all books.",
        }),
      ),
    }),
    async execute(_toolCallId, params: { query: string; limit?: number; sources?: string; corpus?: string }) {
      const q = (params.query ?? "").trim();
      if (!q) {
        return { content: [{ type: "text" as const, text: "offline_search: nothing to search for (the query is empty)" }], isError: true };
      }
      const limit = Math.min(Math.max(Math.round(params.limit ?? 5), 1), 25);

      const url = new URL("/search", SIW_URL);
      url.searchParams.set("q", q);
      url.searchParams.set("limit", String(limit));
      const src = (params.sources ?? "").trim();
      const fam = (params.corpus ?? "").trim();
      if (src) url.searchParams.set("sources", src);
      if (fam) url.searchParams.set("corpus", fam);

      let record;
      try {
        const res = await fetch(url, { signal: AbortSignal.timeout(60_000) });
        record = await res.json();
        if (!res.ok) {
          // The one error shape carries the code and the remedy in its sentence; render it
          // as the answer, not a stack trace. `isError` says to the harness: this did not
          // answer the question.
          const err = record?.error;
          return {
            content: [{
              type: "text" as const,
              text: err ? `offline_search: ${err.code} — ${err.message}` : `offline_search: unexpected ${res.status}`,
            }],
            isError: true,
          };
        }
      } catch (e) {
        // Unreachable-engine is a fact about the machine, not the query: say it plainly.
        return {
          content: [{
            type: "text" as const,
            text: `offline_search: SIW at ${SIW_URL} is not answering (${e instanceof Error ? e.message : e}) — start it with \`siw serve\`; the record says what is wrong.`,
          }],
          isError: true,
        };
      }

      return { content: [{ type: "text" as const, text: render(record, q) }] };
    },
  });
}

/** The rendering is free-form; the record is the product. Keep it compact and honest:
 * say what was searched, what came back, and what the zero means. */
function render(record: any, query: string): string {
  const lines: string[] = [];
  const scope = record?.scope;
  const route = record?.route ?? "?";
  const configured = scope?.configured;
  const corpus = scope?.corpus;
  const served = scope?.served;
  lines.push(
    `searched ${configured ?? "?"} books (${corpus ?? "?"}) — ${route}` +
    (served != null ? `, engine serves ${served}` : "") +
    (record?.healed_in_seconds != null ? `, healed in ${record.healed_in_seconds} s` : ""),
  );
  if (record?.heal_note) lines.push(`  ${record.heal_note}`);
  if (record?.zero_hit) lines.push("", record.zero_hit);
  const results = Array.isArray(record?.results) ? record.results : [];
  for (let i = 0; i < results.length; i++) {
    const r = results[i];
    const urlPart = r?.url ? ` — ${record.endpoint}${r.url}` : "";
    lines.push("", `${i + 1}. ${r?.title ?? "(no title)"} — ${r?.book ?? "?"}${urlPart}`);
    if (r?.snippet) lines.push(`   ${r.snippet}`);
  }
  if (results.length) {
    lines.push("", `${record?.count ?? 0} shown, ${record?.total ?? 0} in index (total for "${query}")`);
  }
  return lines.join("\n");
}
