import { PanelExtensionContext, RenderState, Topic, MessageEvent, Immutable } from "@foxglove/extension";
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";

import { buildRemapLut, applyRemap, RemapLut, CylindricalIntrinsics } from "./dewarp";

interface CompressedImageMessage {
  timestamp: { sec: number; nsec: number };
  frame_id: string;
  data: string; // base64
  format: string;
}

interface CylindricalCameraInfoMessage extends CylindricalIntrinsics {
  frame_id: string;
  projection: string;
}

const IMAGE_SCHEMA = "foxglove.CompressedImage";
const INTRINSICS_SCHEMA = "foxglove_extension.CylindricalCameraInfo";

function base64ToBlob(base64: string, mimeType: string): Blob {
  const binary = atob(base64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) {
    bytes[i] = binary.charCodeAt(i);
  }
  return new Blob([bytes], { type: mimeType });
}

export function CylindricalCameraPanel({ context }: { context: PanelExtensionContext }): JSX.Element {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const workCanvasRef = useRef<OffscreenCanvas | HTMLCanvasElement | undefined>(undefined);
  const lutRef = useRef<RemapLut | undefined>(undefined);
  const lutKeyRef = useRef<string>("");

  const [topics, setTopics] = useState<readonly Topic[]>([]);
  const [imageTopic, setImageTopic] = useState<string | undefined>(undefined);
  const [intrinsicsTopic, setIntrinsicsTopic] = useState<string | undefined>(undefined);
  const [horizontalFovDeg, setHorizontalFovDeg] = useState<number>(90);
  const [renderDone, setRenderDone] = useState<(() => void) | undefined>(undefined);

  const latestIntrinsics = useRef<CylindricalCameraInfoMessage | undefined>(undefined);
  const latestImage = useRef<CompressedImageMessage | undefined>(undefined);

  // Discover available topics and default to the first matching pair.
  useEffect(() => {
    context.watch("topics");
  }, [context]);

  useLayoutEffect(() => {
    context.onRender = (renderState: Immutable<RenderState>, done: () => void) => {
      setRenderDone(() => done);
      if (renderState.topics) {
        setTopics(renderState.topics);
      }
      const messages = renderState.currentFrame;
      if (messages) {
        for (const msg of messages) {
          if (msg.topic === intrinsicsTopic) {
            latestIntrinsics.current = msg.message as CylindricalCameraInfoMessage;
          } else if (msg.topic === imageTopic) {
            latestImage.current = msg.message as CompressedImageMessage;
          }
        }
      }
    };
    context.watch("currentFrame");
  }, [context, imageTopic, intrinsicsTopic]);

  useEffect(() => {
    const subs = [];
    if (imageTopic) subs.push({ topic: imageTopic });
    if (intrinsicsTopic) subs.push({ topic: intrinsicsTopic });
    context.subscribe(subs);
  }, [context, imageTopic, intrinsicsTopic]);

  // Pick sensible topic defaults once topics are known.
  useEffect(() => {
    if (!imageTopic) {
      const firstImage = topics.find((t) => t.schemaName === IMAGE_SCHEMA);
      if (firstImage) setImageTopic(firstImage.name);
    }
    if (!intrinsicsTopic) {
      const firstIntrinsics = topics.find((t) => t.schemaName === INTRINSICS_SCHEMA);
      if (firstIntrinsics) setIntrinsicsTopic(firstIntrinsics.name);
    }
  }, [topics, imageTopic, intrinsicsTopic]);

  // Render loop: decode the latest image, dewarp it using the latest
  // intrinsics, and paint it onto the visible canvas.
  useEffect(() => {
    let cancelled = false;

    async function render() {
      const canvas = canvasRef.current;
      const image = latestImage.current;
      const intrinsics = latestIntrinsics.current;
      if (!canvas || !image || !intrinsics) {
        renderDone?.();
        return;
      }

      const blob = base64ToBlob(image.data, `image/${image.format === "jpeg" ? "jpeg" : image.format}`);
      const bitmap = await createImageBitmap(blob);
      if (cancelled) return;

      const destWidth = canvas.clientWidth || 800;
      const destHeight = Math.round((destWidth * intrinsics.height) / intrinsics.width);
      canvas.width = destWidth;
      canvas.height = destHeight;

      const lutKey = `${intrinsics.fx}:${intrinsics.fy}:${intrinsics.cx}:${intrinsics.cy}:${intrinsics.width}:${intrinsics.height}:${destWidth}:${destHeight}:${horizontalFovDeg}`;
      if (lutKeyRef.current !== lutKey) {
        lutRef.current = buildRemapLut(intrinsics, destWidth, destHeight, horizontalFovDeg);
        lutKeyRef.current = lutKey;
      }

      if (!workCanvasRef.current) {
        workCanvasRef.current = document.createElement("canvas");
      }
      const workCanvas = workCanvasRef.current as HTMLCanvasElement;
      workCanvas.width = bitmap.width;
      workCanvas.height = bitmap.height;
      const workCtx = workCanvas.getContext("2d");
      const destCtx = canvas.getContext("2d");
      if (!workCtx || !destCtx || !lutRef.current) {
        renderDone?.();
        return;
      }
      workCtx.drawImage(bitmap, 0, 0);
      const srcImageData = workCtx.getImageData(0, 0, bitmap.width, bitmap.height);
      const dest = applyRemap(lutRef.current, srcImageData);
      destCtx.putImageData(dest, 0, 0);

      renderDone?.();
    }

    render().catch((err: unknown) => {
      console.error("CylindricalCameraPanel render failed", err);
      renderDone?.();
    });

    return () => {
      cancelled = true;
    };
  }, [renderDone, horizontalFovDeg]);

  const imageTopicOptions = topics.filter((t) => t.schemaName === IMAGE_SCHEMA);
  const intrinsicsTopicOptions = topics.filter((t) => t.schemaName === INTRINSICS_SCHEMA);

  const onImageTopicChange = useCallback((e: React.ChangeEvent<HTMLSelectElement>) => {
    setImageTopic(e.target.value || undefined);
  }, []);
  const onIntrinsicsTopicChange = useCallback((e: React.ChangeEvent<HTMLSelectElement>) => {
    setIntrinsicsTopic(e.target.value || undefined);
  }, []);
  const onFovChange = useCallback((e: React.ChangeEvent<HTMLInputElement>) => {
    const v = Number(e.target.value);
    if (!Number.isNaN(v) && v > 0 && v < 180) setHorizontalFovDeg(v);
  }, []);

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%", padding: 8, gap: 8 }}>
      <div style={{ display: "flex", gap: 16, flexWrap: "wrap", fontSize: 12 }}>
        <label>
          Image topic:{" "}
          <select value={imageTopic ?? ""} onChange={onImageTopicChange}>
            <option value="">(none)</option>
            {imageTopicOptions.map((t) => (
              <option key={t.name} value={t.name}>
                {t.name}
              </option>
            ))}
          </select>
        </label>
        <label>
          Intrinsics topic:{" "}
          <select value={intrinsicsTopic ?? ""} onChange={onIntrinsicsTopicChange}>
            <option value="">(none)</option>
            {intrinsicsTopicOptions.map((t) => (
              <option key={t.name} value={t.name}>
                {t.name}
              </option>
            ))}
          </select>
        </label>
        <label>
          Horizontal FOV (deg):{" "}
          <input type="number" min={1} max={179} value={horizontalFovDeg} onChange={onFovChange} style={{ width: 56 }} />
        </label>
      </div>
      <canvas ref={canvasRef} style={{ width: "100%", flex: 1, imageRendering: "pixelated" }} />
      {!imageTopic || !intrinsicsTopic ? (
        <div style={{ fontSize: 12, opacity: 0.7 }}>
          Select an image topic ({IMAGE_SCHEMA}) and an intrinsics topic ({INTRINSICS_SCHEMA}) above.
        </div>
      ) : undefined}
    </div>
  );
}
