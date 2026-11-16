(() => {
  "use strict";

  const token = document.querySelector('meta[name="app-token"]').content;
  const discList = document.getElementById("disc-list");
  const discListWrap = document.getElementById("disc-list-wrap");
  const discInput = document.getElementById("disc-files");
  const addDiscButton = document.getElementById("add-disc");
  const clearDiscsButton = document.getElementById("clear-discs");
  const titleInput = document.getElementById("game-title");
  const status = document.getElementById("status-message");
  const convertButton = document.getElementById("convert-button");
  const artwork = { icon: null, thumbnail: null, background: null };
  let discs = [];
  let busy = false;
  const artworkDimensions = {
    icon: { width: 80, height: 80, mode: "fit" },
    thumbnail: { width: 310, height: 180, mode: "crop" },
    background: { width: 480, height: 272, mode: "crop" },
  };
  let activeSlot = "icon";

  function setStatus(message, kind = "") {
    status.textContent = message;
    status.dataset.kind = kind;
  }

  function renderDiscs() {
    discList.replaceChildren();
    discListWrap.hidden = discs.length === 0;
    addDiscButton.textContent = `Add Disc ${discs.length + 1}`;
    addDiscButton.disabled = busy || discs.length >= 5;
    discInput.disabled = busy || discs.length >= 5;
    clearDiscsButton.disabled = busy || discs.length === 0;
    discs.forEach((disc, index) => {
      const item = document.createElement("li");
      item.className = "source-item disc-item";
      const number = document.createElement("span");
      number.className = "disc-number";
      number.textContent = `DISC ${index + 1}`;
      const name = document.createElement("span");
      name.className = "source-name";
      name.textContent = disc.name;
      name.title = disc.name;
      const tracks = document.createElement("span");
      tracks.className = "source-size";
      tracks.textContent = `${disc.tracks} tracks · ${disc.binCount} BIN`;
      const controls = document.createElement("span");
      controls.className = "disc-controls";
      for (const [label, text, action] of [
        ["Move disc up", "↑", () => {
          if (index > 0) [discs[index - 1], discs[index]] = [discs[index], discs[index - 1]];
          renderDiscs();
        }],
        ["Move disc down", "↓", () => {
          if (index < discs.length - 1) [discs[index + 1], discs[index]] = [discs[index], discs[index + 1]];
          renderDiscs();
        }],
        ["Remove disc", "×", () => {
          discs.splice(index, 1);
          renderDiscs();
        }],
      ]) {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "disc-control";
        button.setAttribute("aria-label", label);
        button.textContent = text;
        button.disabled = busy;
        button.addEventListener("click", action);
        controls.append(button);
      }
      item.append(number, name, tracks, controls);
      discList.append(item);
    });
  }

  clearDiscsButton.addEventListener("click", () => {
    if (busy) return;
    discs = [];
    renderDiscs();
    setStatus("");
  });

  addDiscButton.addEventListener("click", () => discInput.click());
  discInput.addEventListener("change", async () => {
    const files = Array.from(discInput.files || []);
    discInput.value = "";
    if (!files.length) return;

    if (discs.length >= 5) {
      setStatus("A PBP can contain no more than five discs.", "error");
      return;
    }
    const cueFiles = files.filter((file) => /\.cue$/i.test(file.name));
    const binFiles = files.filter((file) => /\.bin$/i.test(file.name));
    if (cueFiles.length !== 1 || binFiles.length < 1 || cueFiles.length + binFiles.length !== files.length) {
      setStatus("Choose exactly one CUE sheet and all BIN files for this disc.", "error");
      return;
    }
    const names = files.map((file) => file.name.toLowerCase());
    if (new Set(names).size !== names.length) {
      setStatus("The selected disc contains files with duplicate names.", "error");
      return;
    }

    busy = true;
    convertButton.disabled = true;
    renderDiscs();
    const discNumber = discs.length + 1;
    try {
      const sourceIds = [];
      const orderedFiles = [cueFiles[0], ...binFiles];
      for (let index = 0; index < orderedFiles.length; index += 1) {
        const file = orderedFiles[index];
        setStatus(`Uploading Disc ${discNumber}: ${file.name} (${index + 1}/${orderedFiles.length})…`);
        sourceIds.push((await upload(file)).id);
      }
      setStatus(`Checking Disc ${discNumber}…`);
      const response = await fetch("/api/discs", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-App-Token": token },
        body: JSON.stringify({ sourceIds }),
      });
      const disc = await response.json();
      if (!response.ok) throw new Error(disc.error || "Could not validate this disc.");
      discs.push(disc);
      renderDiscs();
      if (!titleInput.value.trim()) {
        titleInput.value = disc.name.replace(/\.cue$/i, "").replace(/[_-]+/g, " ");
      }
      setStatus(`Disc ${discNumber} uploaded and validated.`);
    } catch (error) {
      setStatus(error instanceof Error ? error.message : `Could not upload Disc ${discNumber}.`, "error");
    } finally {
      busy = false;
      convertButton.disabled = false;
      renderDiscs();
    }
  });

  function setArtwork(slot, file) {
    if (!file || !file.type.startsWith("image/")) {
      setStatus("Choose or paste a valid image.", "error");
      return;
    }
    if (artwork[slot]?.previewUrl) URL.revokeObjectURL(artwork[slot].previewUrl);
    const previewUrl = URL.createObjectURL(file);
    const preview = document.getElementById(`preview-${slot}`);
    const canvas = preview.querySelector("canvas");
    const image = new Image();
    const mode = artworkDimensions[slot].mode;
    const state = {
      file,
      previewUrl,
      image,
      mode,
      zoom: 1,
      positionX: 50,
      positionY: 50,
    };
    artwork[slot] = state;
    image.onload = () => {
      if (artwork[slot] !== state) return;
      canvas.hidden = false;
      document.getElementById(`adjust-${slot}`).hidden = false;
      updateArtworkControls(slot);
      drawArtwork(slot);
    };
    image.onerror = () => {
      if (artwork[slot] !== state) return;
      setStatus(`Could not read the ${slot} image. Try another image format.`, "error");
      document.querySelector(`[data-slot="${slot}"]`).querySelector(".remove-image").click();
    };
    image.src = previewUrl;
    preview.querySelector(".preview-placeholder").hidden = true;
    preview.querySelector(".remove-image").hidden = false;
    document.getElementById(`name-${slot}`).textContent = file.name || "Pasted image";
    document.querySelector(`[data-slot="${slot}"]`).classList.add("is-active");
    activeSlot = slot;
    setStatus("");
  }

  function drawArtwork(slot) {
    const state = artwork[slot];
    if (!state?.image.complete || !state.image.naturalWidth) return;
    const canvas = document.querySelector(`#preview-${slot} canvas`);
    const context = canvas.getContext("2d", { alpha: true });
    const { width, height } = artworkDimensions[slot];
    const baseScale = state.mode === "fit"
      ? Math.min(width / state.image.naturalWidth, height / state.image.naturalHeight)
      : Math.max(width / state.image.naturalWidth, height / state.image.naturalHeight);
    const scale = baseScale * (state.mode === "crop" ? state.zoom : 1);
    const drawWidth = state.image.naturalWidth * scale;
    const drawHeight = state.image.naturalHeight * scale;
    const overflowX = Math.max(0, drawWidth - width);
    const overflowY = Math.max(0, drawHeight - height);
    const drawX = -overflowX * state.positionX / 100;
    const drawY = -overflowY * state.positionY / 100;
    context.clearRect(0, 0, width, height);
    context.drawImage(state.image, drawX, drawY, drawWidth, drawHeight);
  }

  function updateArtworkControls(slot) {
    const state = artwork[slot];
    const card = document.querySelector(`[data-slot="${slot}"]`);
    for (const button of card.querySelectorAll(".mode-button")) {
      button.setAttribute("aria-pressed", String(button.dataset.mode === state.mode));
    }
    const slider = card.querySelector(".zoom-slider");
    slider.value = String(Math.round(state.zoom * 100));
    slider.disabled = state.mode !== "crop";
    card.querySelector(".art-preview").classList.toggle("can-pan", state.mode === "crop");
  }

  for (const slot of Object.keys(artwork)) {
    const card = document.querySelector(`[data-slot="${slot}"]`);
    const input = document.getElementById(`image-${slot}`);
    const preview = card.querySelector(".art-preview");
    input.addEventListener("change", () => {
      if (input.files[0]) setArtwork(slot, input.files[0]);
      input.value = "";
    });
    card.addEventListener("click", (event) => {
      if (!event.target.closest("button, label")) activeSlot = slot;
    });
    card.addEventListener("dragover", (event) => {
      event.preventDefault();
      activeSlot = slot;
      card.classList.add("is-dragging");
    });
    card.addEventListener("dragleave", () => card.classList.remove("is-dragging"));
    card.addEventListener("drop", (event) => {
      event.preventDefault();
      card.classList.remove("is-dragging");
      if (event.dataTransfer.files[0]) setArtwork(slot, event.dataTransfer.files[0]);
    });
    card.querySelector(".remove-image").addEventListener("click", () => {
      if (artwork[slot]?.previewUrl) URL.revokeObjectURL(artwork[slot].previewUrl);
      artwork[slot] = null;
      preview.querySelector("canvas").hidden = true;
      preview.querySelector("canvas").getContext("2d").clearRect(
        0,
        0,
        artworkDimensions[slot].width,
        artworkDimensions[slot].height,
      );
      preview.querySelector(".preview-placeholder").hidden = false;
      preview.querySelector(".remove-image").hidden = true;
      document.getElementById(`adjust-${slot}`).hidden = true;
      document.getElementById(`name-${slot}`).textContent = "Drop image here";
      card.classList.remove("is-active");
    });
    card.querySelector(".paste-button").addEventListener("click", () => {
      activeSlot = slot;
      card.classList.add("is-active");
      setStatus(`Copy an image, then press Ctrl+V / ⌘V to paste it as the ${slot}.`);
    });

    for (const button of card.querySelectorAll(".mode-button")) {
      button.addEventListener("click", () => {
        if (!artwork[slot]) return;
        artwork[slot].mode = button.dataset.mode;
        updateArtworkControls(slot);
        drawArtwork(slot);
      });
    }

    card.querySelector(".zoom-slider").addEventListener("input", (event) => {
      if (!artwork[slot]) return;
      artwork[slot].zoom = Number(event.currentTarget.value) / 100;
      drawArtwork(slot);
    });

    let dragPosition = null;
    preview.addEventListener("pointerdown", (event) => {
      const state = artwork[slot];
      if (!state || state.mode !== "crop" || event.target.closest(".remove-image")) return;
      const { width, height } = artworkDimensions[slot];
      const baseScale = Math.max(width / state.image.naturalWidth, height / state.image.naturalHeight);
      const overflowX = Math.max(0, state.image.naturalWidth * baseScale * state.zoom - width);
      const overflowY = Math.max(0, state.image.naturalHeight * baseScale * state.zoom - height);
      if (overflowX === 0 && overflowY === 0) return;
      dragPosition = { x: event.clientX, y: event.clientY };
      preview.setPointerCapture(event.pointerId);
      event.preventDefault();
    });
    preview.addEventListener("pointermove", (event) => {
      if (!dragPosition || !artwork[slot]) return;
      const state = artwork[slot];
      const { width, height } = artworkDimensions[slot];
      const baseScale = Math.max(width / state.image.naturalWidth, height / state.image.naturalHeight);
      const overflowX = Math.max(0, state.image.naturalWidth * baseScale * state.zoom - width);
      const overflowY = Math.max(0, state.image.naturalHeight * baseScale * state.zoom - height);
      const deltaX = (event.clientX - dragPosition.x) * width / preview.clientWidth;
      const deltaY = (event.clientY - dragPosition.y) * height / preview.clientHeight;
      if (overflowX) state.positionX = Math.max(0, Math.min(100, state.positionX - deltaX / overflowX * 100));
      if (overflowY) state.positionY = Math.max(0, Math.min(100, state.positionY - deltaY / overflowY * 100));
      dragPosition = { x: event.clientX, y: event.clientY };
      drawArtwork(slot);
    });
    preview.addEventListener("pointerup", () => { dragPosition = null; });
    preview.addEventListener("pointercancel", () => { dragPosition = null; });
  }

  document.addEventListener("paste", (event) => {
    const imageItem = Array.from(event.clipboardData?.items || []).find((item) => item.type.startsWith("image/"));
    if (!imageItem) return;
    const file = imageItem.getAsFile();
    if (!file) return;
    event.preventDefault();
    const named = new File([file], `pasted-${activeSlot}`, { type: file.type });
    setArtwork(activeSlot, named);
  });

  async function upload(file, name = file.name) {
    const response = await fetch("/api/uploads", {
      method: "PUT",
      headers: { "X-App-Token": token, "X-File-Name": encodeURIComponent(name) },
      body: file,
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || `Could not upload ${name}.`);
    return result;
  }

  function toPng(slot) {
    const canvas = document.querySelector(`#preview-${slot} canvas`);
    return new Promise((resolve, reject) => {
      canvas.toBlob((blob) => {
        if (!blob) {
          reject(new Error(`Could not prepare the ${slot} image.`));
          return;
        }
        resolve(new File([blob], `${slot}.png`, { type: "image/png" }));
      }, "image/png");
    });
  }

  document.getElementById("convert-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (busy) {
      setStatus("Wait for the current disc upload to finish.", "error");
      return;
    }
    if (!discs.length) {
      setStatus("Upload the CUE and BIN files for at least one disc.", "error");
      return;
    }
    if (!titleInput.value.trim()) {
      titleInput.focus();
      setStatus("Enter a game title.", "error");
      return;
    }
    busy = true;
    convertButton.disabled = true;
    renderDiscs();
    setStatus("Preparing artwork…");
    try {
      const selectedDiscs = discs.map((disc) => ({ sourceIds: disc.sourceIds }));
      const uploadedImages = {};
      for (const slot of Object.keys(artwork)) {
        if (!artwork[slot]) continue;
        setStatus(`Preparing ${slot}…`);
        const png = await toPng(slot);
        uploadedImages[slot] = (await upload(png, png.name)).id;
      }
      setStatus("Building EBOOT.PBP — large games may take a few minutes.");
      const response = await fetch("/api/convert", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-App-Token": token },
        body: JSON.stringify({
          title: titleInput.value,
          gameId: document.getElementById("game-id").value,
          discs: selectedDiscs,
          images: uploadedImages,
          compression: Number(document.getElementById("compression").value),
        }),
      });
      if (!response.ok) {
        const error = await response.json();
        throw new Error(error.error || "Conversion failed.");
      }
      const blob = await response.blob();
      const objectUrl = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = objectUrl;
      anchor.download = `${titleInput.value.trim().replace(/[\\/:*?"<>|]+/g, "-")}.pbp`;
      document.body.append(anchor);
      anchor.click();
      anchor.remove();
      URL.revokeObjectURL(objectUrl);
      setStatus("Your PBP is ready and the download has started.", "success");
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "Conversion failed.", "error");
    } finally {
      busy = false;
      convertButton.disabled = false;
      renderDiscs();
    }
  });
})();
