import { useRef, useState } from "react";
import { Archive, ArrowUpFromLine, CheckCircle2, CircleAlert, Clock3, FileCode2, LoaderCircle, ShieldCheck, Terminal, X } from "lucide-react";

const API = "/api";

const ACCEPTED_EXTENSIONS = [".zip", ".py", ".cpp", ".cc", ".cxx", ".java", ".js", ".mjs", ".cjs"];
const ACCEPT = ACCEPTED_EXTENSIONS.join(",");

function messageFrom(payload, fallback) {
  const detail = payload?.detail;
  if (typeof detail === "string" && detail) return detail;
  if (detail) return JSON.stringify(detail);
  return fallback;
}

export default function App() {
  const inputRef = useRef(null);
  const [file, setFile] = useState(null);
  const [dragging, setDragging] = useState(false);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState("");
  const [result, setResult] = useState(null);

  function chooseFile(candidate) {
    setError("");
    setResult(null);
    if (!candidate) return;
    const lower = candidate.name.toLowerCase();
    if (!ACCEPTED_EXTENSIONS.some((ext) => lower.endsWith(ext))) {
      setFile(null);
      setError("Choose a .zip archive or a source file (.py, .cpp, .java, .js).");
      return;
    }
    if (candidate.size > 20 * 1024 * 1024) {
      setFile(null);
      setError("The upload must be 20 MB or smaller.");
      return;
    }
    setFile(candidate);
  }

  async function runProject() {
    if (!file || running) return;
    setRunning(true);
    setError("");
    setResult(null);
    const form = new FormData();
    form.append("file", file);
    try {
      const response = await fetch(`${API}/run`, { method: "POST", body: form });
      let payload = null;
      try {
        payload = await response.json();
      } catch {
        payload = null;
      }
      if (!response.ok && !(payload && payload.status && typeof payload.output === "string")) {
        throw new Error(messageFrom(payload, "The project could not be run."));
      }
      if (!response.ok) {
        setError(messageFrom(payload, "The project could not be run."));
      }
      setResult(payload);
    } catch (err) {
      setError(err.message || "Could not connect to ZipRun. Is the backend running?");
    } finally {
      setRunning(false);
    }
  }

  const statusLabel = result?.status === "success" ? "Completed successfully" :
    result?.status === "timeout" ? "Stopped: time limit reached" :
    result ? "Execution failed" : "";

  return (
    <main className="shell">
      <header className="topbar">
        <a className="brand" href="#"><span className="brand-icon"><Archive size={19}/></span> ziprun<span className="brand-dot">.</span></a>
        <div className="secure-label"><ShieldCheck size={15}/> Docker-isolated execution</div>
      </header>

      <section className="hero">
        <div className="eyebrow"><span className="pulse"/> DEVELOPER TOOLING, SIMPLIFIED</div>
        <h1>From ZIP to <span>output.</span></h1>
        <p className="subtitle">Drop in a project archive or a single source file. ZipRun inspects the entry point, runs supported code in a restricted Docker container, and shows you the result.</p>
      </section>

      <section className="workspace">
        <div className="section-heading"><div><span className="step">01</span><h2>Upload your project</h2></div><span className="file-limit">ZIP or source file · up to 20 MB</span></div>
        <div
          className={`dropzone ${dragging ? "dragging" : ""} ${file ? "has-file" : ""}`}
          onDragOver={(e) => { e.preventDefault(); setDragging(true); }}
          onDragLeave={() => setDragging(false)}
          onDrop={(e) => { e.preventDefault(); setDragging(false); chooseFile(e.dataTransfer.files?.[0]); }}
          onClick={() => !file && inputRef.current?.click()}
          role="button"
          tabIndex={0}
          onKeyDown={(e) => { if ((e.key === "Enter" || e.key === " ") && !file) inputRef.current?.click(); }}
        >
          <input ref={inputRef} type="file" accept={ACCEPT} hidden onChange={(e) => chooseFile(e.target.files?.[0])}/>
          {file ? (
            <div className="selected-file">
              <div className="file-icon"><Archive size={24}/></div>
              <div className="file-info"><strong>{file.name}</strong><span>{(file.size / 1024 / 1024).toFixed(2)} MB · Ready to run</span></div>
              <button className="remove-file" aria-label="Remove file" onClick={(e) => { e.stopPropagation(); setFile(null); setResult(null); if (inputRef.current) inputRef.current.value = ""; }}><X size={17}/></button>
            </div>
          ) : (
            <>
              <div className="upload-icon"><ArrowUpFromLine size={25}/></div>
              <strong>Drag and drop a ZIP or source file here</strong>
              <span>or <button className="browse" onClick={(e) => { e.stopPropagation(); inputRef.current?.click(); }}>browse files</button></span>
              <small>Python · C++ · Java · Node.js</small>
            </>
          )}
        </div>
        {error && <div className="alert error"><CircleAlert size={17}/><span>{error}</span></div>}
        <button className="run-button" disabled={!file || running} onClick={runProject}>
          {running ? <><LoaderCircle className="spin" size={18}/> Inspecting and running…</> : <><Terminal size={18}/> Run project <span>↗</span></>}
        </button>
        <p className="privacy-note"><ShieldCheck size={14}/> Runs offline in a restricted container. Dependencies and browser previews are not enabled in this MVP.</p>
      </section>

      <section className="results">
        <div className="section-heading"><div><span className="step">02</span><h2>Execution output</h2></div>{result && <span className={`status ${result.status}`}><span/> {statusLabel}</span>}</div>
        {!result && !running && (
          <div className="terminal empty-terminal">
            <div className="terminal-bar"><span/><span/><span/><label>ziprun — terminal</label></div>
            <div className="terminal-placeholder"><Terminal size={24}/><p>Your output will appear here</p><small>Upload a project and click Run project to begin.</small></div>
          </div>
        )}
        {running && (
          <div className="terminal">
            <div className="terminal-bar"><span/><span/><span/><label>ziprun — terminal</label></div>
            <div className="terminal-body"><p className="muted-line">$ ziprun inspect upload.zip</p><p><span className="terminal-green">›</span> Validating archive and detecting entry point…</p><p><span className="terminal-green">›</span> Preparing restricted Docker container…</p><p className="cursor-line">Waiting for process output<span className="cursor">▌</span></p></div>
          </div>
        )}
        {result && (
          <div className="terminal">
            <div className="terminal-bar"><span/><span/><span/><label>{result.filename || "project.zip"}</label><span className="runtime-label">{result.language}</span></div>
            <div className="run-meta"><span><FileCode2 size={14}/>{result.language}</span><span><Clock3 size={14}/>{result.duration_seconds}s</span><span>Exit code: {result.exit_code ?? "—"}</span></div>
            <pre className="output">{result.output || (result.status === "success" ? "Process completed with no output." : "No output captured.")}</pre>
            <div className={`result-footer ${result.status}`} >{result.status === "success" ? <CheckCircle2 size={15}/> : <CircleAlert size={15}/>} {statusLabel}</div>
          </div>
        )}
      </section>

      <footer><span>ZIPRUN <span className="footer-sep">/</span> LOCAL MVP</span><span>Built for developers · Treat uploads as untrusted code</span></footer>
    </main>
  );
}