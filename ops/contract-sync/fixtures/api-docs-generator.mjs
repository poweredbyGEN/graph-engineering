// Offline stand-in for the api-docs command contracts. The bot must pass the
// immutable sources through these actual subprocess seams, not a default repo.
import { execFileSync } from "node:child_process";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";

const args = process.argv.slice(2);
const option = (name) => {
  const index = args.indexOf(name);
  if (index < 0 || !args[index + 1]) throw new Error(`missing ${name}`);
  return args[index + 1];
};
const revision = (repo, ref = "HEAD") =>
  execFileSync("git", ["-C", repo, "rev-parse", ref], { encoding: "utf8" }).trim();
const record = (repo, ref, name) =>
  execFileSync("git", ["-C", repo, "show", `${ref}:src/gen_mcp_server/contracts/catalog-record/${name}.tsv`], { encoding: "utf8" }).trim();
const write = (file, content) => {
  mkdirSync(path.dirname(file), { recursive: true });
  writeFileSync(file, content);
};
if (process.env.FIXTURE_DOCS_FAIL === "1") throw new Error("fixture generator failure");
if (process.argv[1].endsWith("update-mcp-tools-snapshot.mjs")) {
  if (!args.includes("--offline")) throw new Error("snapshot must be offline");
  const repo = option("--aliases-repo");
  const ref = option("--ref");
  const sha = revision(repo, ref);
  if (ref !== sha || sha !== revision(repo)) throw new Error("snapshot ref is not exact HEAD");
  write("scripts/mcp-tools-snapshot.json", JSON.stringify({
    source: sha,
    served: record(repo, ref, "schema-paths"),
    generated_at: new Date().toISOString(),
  }) + "\n");
} else {
  const backend = option("--backend");
  const mcp = option("--mcp-repo");
  const sha = revision(mcp, "origin/main");
  if (sha !== revision(mcp)) throw new Error("registry ref is not exact HEAD");
  const snapshot = JSON.parse(readFileSync("scripts/mcp-tools-snapshot.json", "utf8"));
  if (snapshot.source !== sha) throw new Error("snapshot did not precede registry generation");
  const content = JSON.stringify({
    backend: revision(backend),
    mcp: sha,
    tools: record(mcp, "origin/main", "schema-paths"),
    routes: record(mcp, "origin/main", "contract-paths"),
  }) + "\n";
  for (const file of ["public/openapi.yaml", "public/.well-known/openapi.yaml", "public/llms.txt", "public/llms-full.txt", "scripts/backend/mcp-tools.json"]) write(file, content);
}
