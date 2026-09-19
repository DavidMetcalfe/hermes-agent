// @vitest-environment jsdom
// The Dashboard persists its active theme server-side; another surface
// (Desktop, another tab) can change it while this tab is hidden. These
// tests pin the refetch-on-refocus contract: adopt the server's active
// name when the tab becomes visible again, and never write back — a PUT
// from the read path would loop with whoever changed it.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";

const apiMocks = vi.hoisted(() => ({
  getThemes: vi.fn(),
  setTheme: vi.fn(),
  getFontPref: vi.fn(),
  setFontPref: vi.fn(),
}));

vi.mock("@/lib/api", () => ({ api: apiMocks }));

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT =
  true;

// jsdom has no `CSS` interface; applyTheme reaches CSS.escape when a theme
// carries a webfont URL. Minimal stand-in — we assert on state, not hrefs.
if (typeof (globalThis as { CSS?: unknown }).CSS === "undefined") {
  (globalThis as { CSS?: { escape: (s: string) => string } }).CSS = {
    escape: (s: string) => s.replace(/["\\]/g, "\\$&"),
  };
}

const STORAGE_KEY = "hermes-dashboard-theme";

let container: HTMLDivElement;
let root: Root;

function Probe() {
  // Imported lazily below via the provider's own context hook.
  const { useTheme } = probeHooks;
  const { themeName } = useTheme();
  return <span>{themeName}</span>;
}

const probeHooks: { useTheme: () => { themeName: string } } = {
  useTheme: () => {
    throw new Error("not initialised");
  },
};

/** Drain the mock promise chains (.then/.catch/.finally) inside act(). */
async function flush() {
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
}

async function renderProvider(initialActive: string) {
  apiMocks.getThemes.mockResolvedValue({ active: initialActive, themes: [] });
  const { ThemeProvider, useTheme } = await import("./context");
  probeHooks.useTheme = useTheme as unknown as () => { themeName: string };
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  await act(async () => {
    root.render(
      <ThemeProvider>
        <Probe />
      </ThemeProvider>,
    );
  });
  await flush();
}

function servedActive(active: string) {
  apiMocks.getThemes.mockResolvedValue({ active, themes: [] });
}

function fireFocus() {
  act(() => {
    window.dispatchEvent(new Event("focus"));
  });
}

async function fireFocusAndSettle() {
  fireFocus();
  await flush();
}

beforeEach(() => {
  localStorage.clear();
  apiMocks.getThemes.mockReset();
  apiMocks.setTheme.mockReset();
  apiMocks.getThemes.mockResolvedValue({ active: "default", themes: [] });
  apiMocks.getFontPref.mockReset();
  apiMocks.getFontPref.mockResolvedValue({ font: "" });
  apiMocks.setFontPref.mockReset();
  apiMocks.setFontPref.mockResolvedValue({ ok: true, font: "" });
});

afterEach(() => {
  act(() => {
    root?.unmount();
  });
  container?.remove();
});

describe("ThemeProvider refetch-on-refocus", () => {
  it("adopts a changed server active theme when the window regains focus", async () => {
    await renderProvider("default");
    expect(container.textContent).toBe("default");

    servedActive("nous-blue");
    await fireFocusAndSettle();

    expect(container.textContent).toBe("nous-blue");
    expect(localStorage.getItem(STORAGE_KEY)).toBe("nous-blue");
  });

  it("adopts on visibilitychange and never writes back to the server", async () => {
    await renderProvider("default");

    servedActive("midnight");
    act(() => {
      document.dispatchEvent(new Event("visibilitychange"));
    });
    await flush();

    expect(container.textContent).toBe("midnight");
    // The refetch path is read/apply only; a PUT here would loop with the
    // surface that made the change.
    expect(apiMocks.setTheme).not.toHaveBeenCalled();
  });

  it("migrates a legacy alias on the focus path without pushing it back", async () => {
    await renderProvider("default");

    // Server still persists the pre-rename key.
    servedActive("lens-5i");
    await fireFocusAndSettle();

    expect(container.textContent).toBe("nous-blue");
    expect(localStorage.getItem(STORAGE_KEY)).toBe("nous-blue");
    // Migration PUTs are mount-only behaviour; the focus path must not
    // re-persist, even when it adopts a migrated name.
    expect(apiMocks.setTheme).not.toHaveBeenCalled();
  });

  it("still pushes a migrated legacy value back on mount", async () => {
    // Guards the shared-helper refactor: the mount path keeps its
    // migration-only write that the focus path deliberately lacks.
    await renderProvider("lens-5i");

    expect(container.textContent).toBe("nous-blue");
    expect(apiMocks.setTheme).toHaveBeenCalledWith("nous-blue");
  });

  it("does not refetch while the document is hidden", async () => {
    await renderProvider("default");
    const callsAfterMount = apiMocks.getThemes.mock.calls.length;

    const visibilityState = Object.getOwnPropertyDescriptor(document, "visibilityState");
    Object.defineProperty(document, "visibilityState", {
      configurable: true,
      get: () => "hidden",
    });
    try {
      fireFocus();
      await flush();
    } finally {
      if (visibilityState) Object.defineProperty(document, "visibilityState", visibilityState);
    }

    expect(apiMocks.getThemes.mock.calls.length).toBe(callsAfterMount);
    expect(container.textContent).toBe("default");
  });
});
