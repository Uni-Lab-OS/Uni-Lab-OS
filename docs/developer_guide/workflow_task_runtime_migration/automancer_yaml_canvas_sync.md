# Automancer PCRL editor and graph synchronization

## Scope

This note reviews the current default branch of
[`adaptyvbio/automancer`](https://github.com/adaptyvbio/automancer) at commit
[`d577cc1775767b5d4839f908410ca5c4a0dadb9f`](https://github.com/adaptyvbio/automancer/commit/d577cc1775767b5d4839f908410ca5c4a0dadb9f).
The repository calls its YAML-like language PCRL. The official documentation
also describes protocols as PCRL text files and the built-in Protocol Editor as
an edit/preview/execute surface:
[protocol format](https://automancer.adaptyvbio.com/usage/what-is/) and
[Protocol Editor](https://automancer.adaptyvbio.com/usage/getting-started-app/).

## Executive result

Automancer does **not** implement bidirectional synchronization between an
editable YAML document and an editable workflow canvas. Its production path is
one-way:

```text
Monaco PCRL buffer
  -> compileDraft(current in-memory contents)
  -> Python parser and language analysis
  -> exported Protocol block tree
  -> read-only GraphEditor projection
```

The text buffer is the authoring source. The graph is a derived preview and
selection/inspection surface. There is no production graph-to-PCRL generator,
graph mutation callback, Apply transaction, or separate persisted Candidate.

## Text-to-graph flow

1. Each opened document owns a Monaco model plus `unsaved`, `writing`, and
   `updated` frontend flags. A Monaco content change marks the document
   `unsaved` and `updated`; optional automatic save is debounced for five
   seconds:
   [`client/src/views/draft.tsx#L82-L150`](https://github.com/adaptyvbio/automancer/blob/d577cc1775767b5d4839f908410ca5c4a0dadb9f/client/src/views/draft.tsx#L82-L150).
2. Marker refresh is debounced for 300 ms. It asks the owning draft view for a
   compilation:
   [`client/src/components/text-editor.tsx#L44-L48`](https://github.com/adaptyvbio/automancer/blob/d577cc1775767b5d4839f908410ca5c4a0dadb9f/client/src/components/text-editor.tsx#L44-L48) and
   [`#L114-L183`](https://github.com/adaptyvbio/automancer/blob/d577cc1775767b5d4839f908410ca5c4a0dadb9f/client/src/components/text-editor.tsx#L114-L183).
3. `getCompilation()` reuses the current promise while no document changed,
   abandons an older result when a newer compilation begins, and sends the
   current **in-memory** contents of every document in `compileDraft`:
   [`client/src/views/draft.tsx#L329-L408`](https://github.com/adaptyvbio/automancer/blob/d577cc1775767b5d4839f908410ca5c4a0dadb9f/client/src/views/draft.tsx#L329-L408).
   Compilation therefore does not require a prior file save.
4. The Python host deserializes the submitted Draft and compiles it:
   [`host/pr1/host.py#L290-L321`](https://github.com/adaptyvbio/automancer/blob/d577cc1775767b5d4839f908410ca5c4a0dadb9f/host/pr1/host.py#L290-L321).
   `Draft.compile()` constructs the parser and returns language analysis plus
   an optional Protocol:
   [`host/pr1/draft.py#L15-L40`](https://github.com/adaptyvbio/automancer/blob/d577cc1775767b5d4839f908410ca5c4a0dadb9f/host/pr1/draft.py#L15-L40).
   The parser reads the PCRL source, accumulates errors/warnings, validates the
   root structure, and produces the block tree:
   [`host/pr1/fiber/parser.py#L507-L683`](https://github.com/adaptyvbio/automancer/blob/d577cc1775767b5d4839f908410ca5c4a0dadb9f/host/pr1/fiber/parser.py#L507-L683).
5. The draft view passes only `compilation.protocol.root` into `GraphEditor`:
   [`client/src/views/draft.tsx#L600-L624`](https://github.com/adaptyvbio/automancer/blob/d577cc1775767b5d4839f908410ca5c4a0dadb9f/client/src/views/draft.tsx#L600-L624).
   The graph recursively invokes plugin block renderers to derive geometry and
   SVG nodes:
   [`client/src/components/graph-editor.tsx#L253-L397`](https://github.com/adaptyvbio/automancer/blob/d577cc1775767b5d4839f908410ca5c4a0dadb9f/client/src/components/graph-editor.tsx#L253-L397).

## Why the graph is not a second editor

`GraphEditorProps` contains the Protocol root, selection callbacks, and an
optional “edit draft” callback, but no graph-change callback:
[`client/src/components/graph-editor.tsx#L16-L29`](https://github.com/adaptyvbio/automancer/blob/d577cc1775767b5d4839f908410ca5c4a0dadb9f/client/src/components/graph-editor.tsx#L16-L29).
Its edit control is hard-disabled with `false && ...`:
[`client/src/components/graph-editor.tsx#L443-L451`](https://github.com/adaptyvbio/automancer/blob/d577cc1775767b5d4839f908410ca5c4a0dadb9f/client/src/components/graph-editor.tsx#L443-L451).
Graph interaction selects block paths and drives the inspector; it does not
rewrite PCRL.

The repository also contains `components/visual-editor.tsx`, but no production
component imports or renders it. Its constructor creates hard-coded Stage A/B
demo data rather than reading the supplied Draft:
[`client/src/components/visual-editor.tsx#L63-L118`](https://github.com/adaptyvbio/automancer/blob/d577cc1775767b5d4839f908410ca5c4a0dadb9f/client/src/components/visual-editor.tsx#L63-L118).
It is an unintegrated UI prototype, not evidence of graph-to-text
synchronization.

## Save and execution

Manual save or the optional five-second autosave writes the complete Monaco
contents to the document. The file is saved even when compilation has errors:
[`client/src/views/draft.tsx#L207-L243`](https://github.com/adaptyvbio/automancer/blob/d577cc1775767b5d4839f908410ca5c4a0dadb9f/client/src/views/draft.tsx#L207-L243).
The UI disables Start/Update while the compilation is invalid:
[`client/src/views/draft.tsx#L824-L912`](https://github.com/adaptyvbio/automancer/blob/d577cc1775767b5d4839f908410ca5c4a0dadb9f/client/src/views/draft.tsx#L824-L912).

Starting a protocol submits the current in-memory document contents again:
[`client/src/views/draft.tsx#L668-L698`](https://github.com/adaptyvbio/automancer/blob/d577cc1775767b5d4839f908410ca5c4a0dadb9f/client/src/views/draft.tsx#L668-L698).
The host recompiles that Draft immediately before constructing the running
Master:
[`host/pr1/host.py#L380-L393`](https://github.com/adaptyvbio/automancer/blob/d577cc1775767b5d4839f908410ca5c4a0dadb9f/host/pr1/host.py#L380-L393).
There is no Uni-Lab-style Applied Workflow revision or immutable WorkflowTask
snapshot boundary in this path.

## External file changes

Electron uses Chokidar with a 500 ms `awaitWriteFinish` stability threshold,
serializes per-file reads/writes, tracks modification times, and reports add,
change, and unlink notifications:
[`app/electron/src/file-manager.ts#L25-L52`](https://github.com/adaptyvbio/automancer/blob/d577cc1775767b5d4839f908410ca5c4a0dadb9f/app/electron/src/file-manager.ts#L25-L52) and
[`#L100-L159`](https://github.com/adaptyvbio/automancer/blob/d577cc1775767b5d4839f908410ca5c4a0dadb9f/app/electron/src/file-manager.ts#L100-L159).
Its own writes update `lastModificationDate` but not
`lastExternalModificationDate`, allowing the renderer to distinguish an
internal save from an external change:
[`app/electron/src/file-manager.ts#L219-L253`](https://github.com/adaptyvbio/automancer/blob/d577cc1775767b5d4839f908410ca5c4a0dadb9f/app/electron/src/file-manager.ts#L219-L253).
The browser backend uses the File System Access API and polls once per second:
[`client/src/app-backends/browser.ts#L84-L153`](https://github.com/adaptyvbio/automancer/blob/d577cc1775767b5d4839f908410ca5c4a0dadb9f/client/src/app-backends/browser.ts#L84-L153).

However, the draft view replaces the Monaco model whenever
`lastExternalModificationDate === lastModificationDate`, then unconditionally
sets `unsaved: false`:
[`client/src/views/draft.tsx#L110-L130`](https://github.com/adaptyvbio/automancer/blob/d577cc1775767b5d4839f908410ca5c4a0dadb9f/client/src/views/draft.tsx#L110-L130).
There is no `unsaved` guard, content hash CAS, conflict state, diff, or merge.
An external file edit can therefore replace unsaved in-app text. The scheme
also identifies changes by modification time rather than exact content hash.

## Consequences for Uni-Lab

Useful patterns to adopt:

- keep a single source buffer for each compilation request;
- compile unsaved in-memory text for fast preview;
- debounce diagnostics separately from persistence;
- suppress stale compilation responses;
- wait for file writes to stabilize and serialize per-Workflow file handling;
- make graph rendering a pure projection from a validated protocol model;
- allow plugin/action types to contribute graph renderers.

Patterns not to adopt:

- replacing a dirty editor on an external file event;
- using file modification time as a concurrency token;
- treating a read-only graph projection as bidirectional synchronization;
- running arbitrary current editor text without an explicit Apply boundary;
- omitting the Applied/Candidate distinction and immutable execution snapshot;
- relying on local Electron IPC notifications without durable replay.

For the Uni-Lab migration, Automancer supports the already selected direction
`Python Draft -> compile -> Candidate DAG`, but it does not solve
`DAG edit -> deterministic Python`, Draft/Workflow dual CAS, conflict review,
atomic Apply, or WorkflowTask snapshotting. Those Uni-Lab contracts remain
necessary.
