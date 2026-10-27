// Anima Prompt Studio: reliable "Paste from clipboard" for the extension's image field.
// Gradio's own clipboard button fails silently when the browser blocks clipboard
// access or the clipboard holds no image. This handles the click itself, loads the
// image through the normal upload input and tells the user what went wrong otherwise.

(function () {
    "use strict";

    const ROOT_SELECTOR = '[id^="anima_img_"]';

    function isClipboardControl(button) {
        if (button.getAttribute("aria-label") === "Paste from clipboard") return true;
        // the big drop area while the clipboard source is selected
        return !!button.closest(".upload-container") && /paste from clipboard/i.test(button.textContent || "");
    }

    function showNote(root, text) {
        let note = root.querySelector(".anima-paste-note");
        if (!note) {
            note = document.createElement("div");
            note.className = "anima-paste-note";
            note.style.cssText =
                "position:absolute;left:4px;right:4px;bottom:4px;z-index:20;padding:4px 6px;" +
                "border-radius:4px;font-size:12px;line-height:1.3;" +
                "background:var(--background-fill-secondary);color:var(--body-text-color);" +
                "border:1px solid var(--border-color-primary);";
            root.appendChild(note);
        }
        note.textContent = text;
        clearTimeout(note._timer);
        note._timer = setTimeout(() => note.remove(), 7000);
    }

    function nextFrame() {
        return new Promise((resolve) => requestAnimationFrame(() => resolve()));
    }

    async function fileInputOf(root) {
        let input = root.querySelector('input[type="file"]');
        if (input) return input;
        // in clipboard mode Gradio removes the input: switch back to "Upload file"
        root.querySelector('button[aria-label="Upload file"]')?.click();
        for (let i = 0; i < 20 && !input; i++) {
            await nextFrame();
            input = root.querySelector('input[type="file"]');
        }
        return input;
    }

    async function readClipboardImage() {
        const items = await navigator.clipboard.read();
        for (const item of items) {
            const type = item.types.find((t) => t.startsWith("image/"));
            if (type) {
                const blob = await item.getType(type);
                return new File([blob], "clipboard." + type.replace("image/", ""), { type });
            }
        }
        return null;
    }

    async function pasteInto(root) {
        if (!navigator.clipboard || !navigator.clipboard.read) {
            showNote(root, "This browser can't read the clipboard here. Click the page and press Ctrl+V instead.");
            return;
        }

        let file;
        try {
            file = await readClipboardImage();
        } catch (err) {
            const blocked = err && err.name === "NotAllowedError";
            showNote(
                root,
                blocked
                    ? "Clipboard access is blocked. Allow it in the site settings (icon left of the address bar), or click the page and press Ctrl+V."
                    : "Couldn't read the clipboard. Click the page and press Ctrl+V instead.",
            );
            return;
        }

        if (!file) {
            showNote(root, "No image in the clipboard. If you copied a file, click the page and press Ctrl+V.");
            return;
        }

        const input = await fileInputOf(root);
        if (!input) {
            showNote(root, "Couldn't load the image. Click the page and press Ctrl+V instead.");
            return;
        }
        const data = new DataTransfer();
        data.items.add(file);
        input.files = data.files;
        input.dispatchEvent(new Event("change"));
    }

    document.addEventListener(
        "click",
        (e) => {
            const button = e.composedPath()[0]?.closest?.("button");
            if (!button) return;
            const root = button.closest(ROOT_SELECTOR);
            if (!root || !isClipboardControl(button)) return;

            e.preventDefault();
            e.stopImmediatePropagation();
            pasteInto(root);
        },
        true,
    );
})();
