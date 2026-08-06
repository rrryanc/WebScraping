import { ExtensionContext } from "@foxglove/extension";
import { createRoot } from "react-dom/client";
import { createElement } from "react";

import { CylindricalCameraPanel } from "./CylindricalCameraPanel";

export function activate(extensionContext: ExtensionContext): void {
  extensionContext.registerPanel({
    name: "Cylindrical Camera View",
    initPanel: (context) => {
      const root = createRoot(context.panelElement);
      root.render(createElement(CylindricalCameraPanel, { context }));
      return () => {
        root.unmount();
      };
    },
  });
}
