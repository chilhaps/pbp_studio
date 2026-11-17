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
  const snd0Input = document.getElementById("snd0-file");
  const snd0Name = document.getElementById("name-snd0");
  const removeSnd0Button = document.getElementById("remove-snd0");
  const artwork = { icon: null, thumbnail: null, background: null };
  const artworkLabels = { icon: "game cover", thumbnail: "logo", background: "background" };
  let discs = [];
  let snd0File = null;
  let busy = false;
  const previewWidth = 384;
  const previewHeight = 216;
  const popfeArtworkWidth = 382;
  const artworkPreview = document.getElementById("artwork-preview");
  const previewContext = artworkPreview.getContext("2d");
  const logoSizeInput = document.getElementById("logo-size");
  const logoSizeOutput = document.getElementById("logo-size-value");
  const composedBackground = document.createElement("canvas");
  composedBackground.width = 480;
  composedBackground.height = 272;
  const composedBackgroundContext = composedBackground.getContext("2d");
  const logoPosition = { x: 148, y: 79 };
  const logoBaseWidth = Math.trunc(popfeArtworkWidth * 0.55);
  const logoBaseHeight = Math.trunc(previewHeight * 0.58);
  const coverPreviewSize = Math.trunc(previewHeight * 0.25);
  let logoScale = 1;
  let logoDrag = null;
  const artworkDimensions = {
    icon: { width: 80, height: 80, mode: "fit" },
    thumbnail: { width: 310, height: 180, mode: "crop" },
    background: { width: 480, height: 272, mode: "crop" },
  };
  let activeSlot = "icon";

  snd0Input.addEventListener("change", () => {
    const file = snd0Input.files[0];
    snd0Input.value = "";
    if (!file) return;
    if (!/\.(?:at3|snd0)$/i.test(file.name)) {
      setStatus("Choose an .AT3 or .SND0 audio file.", "error");
      return;
    }
    if (file.size > 64 * 1024 * 1024) {
      setStatus("SND0 audio must be no larger than 64 MB.", "error");
      return;
    }
    snd0File = file;
    snd0Name.textContent = file.name;
    snd0Name.title = file.name;
    removeSnd0Button.hidden = false;
    setStatus("");
  });

  removeSnd0Button.addEventListener("click", () => {
    snd0File = null;
    snd0Name.textContent = "No audio selected";
    snd0Name.removeAttribute("title");
    removeSnd0Button.hidden = true;
  });

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
      setStatus(`Could not read the ${artworkLabels[slot]} image. Try another image format.`, "error");
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
    drawArtworkPreview();
  }

  function drawComposedBackground() {
    composedBackgroundContext.clearRect(0, 0, composedBackground.width, composedBackground.height);
    const background = artwork.background;
    if (background?.image.complete && background.image.naturalWidth) {
      composedBackgroundContext.drawImage(
        document.querySelector("#preview-background canvas"),
        0,
        0,
        composedBackground.width,
        composedBackground.height,
      );
    }

    const logo = artwork.thumbnail;
    if (logo?.image.complete && logo.image.naturalWidth) {
      const { width, height } = getLogoPreviewSize();
      composedBackgroundContext.drawImage(
        document.querySelector("#preview-thumbnail canvas"),
        logoPosition.x * composedBackground.width / previewWidth,
        logoPosition.y * composedBackground.height / previewHeight,
        width * composedBackground.width / previewWidth,
        height * composedBackground.height / previewHeight,
      );
    }
  }

  function getLogoPreviewSize() {
    return { width: logoBaseWidth * logoScale, height: logoBaseHeight * logoScale };
  }

  function drawArtworkPreview() {
    drawComposedBackground();
    previewContext.clearRect(0, 0, artworkPreview.width, artworkPreview.height);
    previewContext.fillStyle = "#fff";
    previewContext.fillRect(0, 0, artworkPreview.width, artworkPreview.height);
    previewContext.drawImage(composedBackground, 0, 0, previewWidth, previewHeight);

    const cover = artwork.icon;
    if (cover?.image.complete && cover.image.naturalWidth) {
      previewContext.drawImage(
        document.querySelector("#preview-icon canvas"),
        36,
        81,
        coverPreviewSize,
        coverPreviewSize,
      );
    }
    artworkPreview.classList.toggle("has-logo", Boolean(artwork.thumbnail));
    logoSizeInput.disabled = !artwork.thumbnail;
  }

  function previewPoint(event) {
    const bounds = artworkPreview.getBoundingClientRect();
    return {
      x: (event.clientX - bounds.left) * previewWidth / bounds.width,
      y: (event.clientY - bounds.top) * previewHeight / bounds.height,
    };
  }

  artworkPreview.addEventListener("pointerdown", (event) => {
    if (!artwork.thumbnail) return;
    const point = previewPoint(event);
    const { width, height } = getLogoPreviewSize();
    if (
      point.x < logoPosition.x
      || point.x > logoPosition.x + width
      || point.y < logoPosition.y
      || point.y > logoPosition.y + height
    ) return;
    logoDrag = { offsetX: point.x - logoPosition.x, offsetY: point.y - logoPosition.y };
    artworkPreview.setPointerCapture(event.pointerId);
    event.preventDefault();
  });
  artworkPreview.addEventListener("pointermove", (event) => {
    if (!logoDrag) return;
    const point = previewPoint(event);
    const { width, height } = getLogoPreviewSize();
    logoPosition.x = Math.max(0, Math.min(previewWidth - width, point.x - logoDrag.offsetX));
    logoPosition.y = Math.max(0, Math.min(previewHeight - height, point.y - logoDrag.offsetY));
    drawArtworkPreview();
  });
  artworkPreview.addEventListener("pointerup", () => { logoDrag = null; });
  artworkPreview.addEventListener("pointercancel", () => { logoDrag = null; });
  artworkPreview.addEventListener("keydown", (event) => {
    if (!artwork.thumbnail || !["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"].includes(event.key)) return;
    const step = event.shiftKey ? 10 : 1;
    if (event.key === "ArrowLeft") logoPosition.x -= step;
    if (event.key === "ArrowRight") logoPosition.x += step;
    if (event.key === "ArrowUp") logoPosition.y -= step;
    if (event.key === "ArrowDown") logoPosition.y += step;
    const { width, height } = getLogoPreviewSize();
    logoPosition.x = Math.max(0, Math.min(previewWidth - width, logoPosition.x));
    logoPosition.y = Math.max(0, Math.min(previewHeight - height, logoPosition.y));
    drawArtworkPreview();
    event.preventDefault();
  });
  logoSizeInput.addEventListener("input", () => {
    const previousSize = getLogoPreviewSize();
    const centerX = logoPosition.x + previousSize.width / 2;
    const centerY = logoPosition.y + previousSize.height / 2;
    logoScale = Number(logoSizeInput.value) / 100;
    const nextSize = getLogoPreviewSize();
    logoPosition.x = Math.max(0, Math.min(previewWidth - nextSize.width, centerX - nextSize.width / 2));
    logoPosition.y = Math.max(0, Math.min(previewHeight - nextSize.height, centerY - nextSize.height / 2));
    logoSizeOutput.value = `${logoSizeInput.value}%`;
    drawArtworkPreview();
  });

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
      drawArtworkPreview();
    });
    card.querySelector(".paste-button").addEventListener("click", () => {
      activeSlot = slot;
      card.classList.add("is-active");
      setStatus(`Copy an image, then press Ctrl+V / ⌘V to paste it as the ${artworkLabels[slot]}.`);
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
    const name = slot === "icon" ? "game-cover.png" : slot === "thumbnail" ? "logo.png" : "background.png";
    return canvasToPng(canvas, name);
  }

  function canvasToPng(canvas, name) {
    return new Promise((resolve, reject) => {
      canvas.toBlob((blob) => {
        if (!blob) {
          reject(new Error(`Could not prepare ${name}.`));
          return;
        }
        resolve(new File([blob], name, { type: "image/png" }));
      }, "image/png");
    });
  }

  drawArtworkPreview();

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
      let snd0Id = null;
      for (const slot of ["icon", "thumbnail"]) {
        if (!artwork[slot]) continue;
        setStatus(`Preparing ${artworkLabels[slot]}…`);
        const png = await toPng(slot);
        uploadedImages[slot] = (await upload(png, png.name)).id;
      }
      if (artwork.background || artwork.thumbnail) {
        setStatus("Preparing PSP background…");
        const png = await canvasToPng(composedBackground, "background.png");
        uploadedImages.background = (await upload(png, png.name)).id;
      }
      if (snd0File) {
        setStatus("Preparing optional XMB audio…");
        snd0Id = (await upload(snd0File)).id;
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
          snd0: snd0Id,
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
