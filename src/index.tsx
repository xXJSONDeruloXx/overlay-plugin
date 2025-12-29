import {
  ButtonItem,
  PanelSection,
  PanelSectionRow,
  Dropdown,
  staticClasses,
  Spinner
} from "@decky/ui";
import {
  callable,
  definePlugin,
  toaster,
} from "@decky/api"
import { useState, useEffect } from "react";
import { FaLayerGroup, FaRocket, FaTimes, FaSync } from "react-icons/fa";

// Types
interface FlatpakApp {
  app_id: string;
  name: string;
}

interface RunningOverlay {
  app_id: string;
  pid: number;
  window_ids: string[];
}

interface LaunchResult {
  success: boolean;
  app_id?: string;
  pid?: number;
  window_id?: string;
  error?: string;
  warning?: string;
}

interface RefreshResult {
  success: boolean;
  app_id?: string;
  total_windows?: number;
  new_windows?: number;
  error?: string;
}

interface DependencyCheck {
  success: boolean;
  dependencies: {
    flatpak: boolean;
    xdotool: boolean;
    xprop: boolean;
  };
}

// Backend callables
const listInstalledFlatpaks = callable<[], FlatpakApp[]>("list_installed_flatpaks");
const launchFlatpakOverlay = callable<[app_id: string], LaunchResult>("launch_flatpak_overlay");
const closeFlatpakOverlay = callable<[app_id: string], { success: boolean; error?: string }>("close_flatpak_overlay");
const getRunningOverlays = callable<[], RunningOverlay[]>("get_running_overlays");
const checkDependencies = callable<[], DependencyCheck>("check_dependencies");
const refreshOverlayWindows = callable<[app_id: string], RefreshResult>("refresh_overlay_windows");

function Content() {
  const [flatpaks, setFlatpaks] = useState<FlatpakApp[]>([]);
  const [selectedAppId, setSelectedAppId] = useState<string | null>(null);
  const [runningOverlays, setRunningOverlays] = useState<RunningOverlay[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [isLaunching, setIsLaunching] = useState(false);
  const [closingApps, setClosingApps] = useState<Set<string>>(new Set());
  const [depsOk, setDepsOk] = useState(true);

  // Load flatpaks and check dependencies on mount
  useEffect(() => {
    const init = async () => {
      setIsLoading(true);
      try {
        // Check dependencies first
        const deps = await checkDependencies();
        setDepsOk(deps.success);
        if (!deps.success) {
          const missing = Object.entries(deps.dependencies)
            .filter(([_, ok]) => !ok)
            .map(([name]) => name);
          toaster.toast({
            title: "Missing Dependencies",
            body: `Required tools not found: ${missing.join(", ")}`
          });
        }

        // Load flatpaks
        const apps = await listInstalledFlatpaks();
        setFlatpaks(apps);
        if (apps.length > 0 && !selectedAppId) {
          setSelectedAppId(apps[0].app_id);
        }

        // Load running overlays
        const running = await getRunningOverlays();
        setRunningOverlays(running);
      } catch (e) {
        console.error("Failed to initialize:", e);
        toaster.toast({
          title: "Error",
          body: "Failed to load flatpak list"
        });
      }
      setIsLoading(false);
    };
    init();
  }, []);

  // Refresh running overlays periodically
  useEffect(() => {
    const interval = setInterval(async () => {
      try {
        const running = await getRunningOverlays();
        setRunningOverlays(running);
      } catch (e) {
        console.error("Failed to refresh running overlays:", e);
      }
    }, 5000);
    return () => clearInterval(interval);
  }, []);

  const handleLaunch = async () => {
    if (!selectedAppId || isLaunching) return;

    setIsLaunching(true);
    try {
      const result = await launchFlatpakOverlay(selectedAppId);
      if (result.success) {
        toaster.toast({
          title: "Overlay Launched",
          body: `${selectedAppId} is now running as an overlay`
        });
        if (result.warning) {
          toaster.toast({
            title: "Warning",
            body: result.warning
          });
        }
        // Refresh running overlays
        const running = await getRunningOverlays();
        setRunningOverlays(running);
      } else {
        toaster.toast({
          title: "Launch Failed",
          body: result.error || "Unknown error"
        });
      }
    } catch (e) {
      console.error("Launch error:", e);
      toaster.toast({
        title: "Error",
        body: "Failed to launch overlay"
      });
    }
    setIsLaunching(false);
  };

  const handleClose = async (appId: string) => {
    setClosingApps(prev => new Set(prev).add(appId));
    try {
      const result = await closeFlatpakOverlay(appId);
      if (result.success) {
        toaster.toast({
          title: "Overlay Closed",
          body: `${appId} has been closed`
        });
        // Refresh running overlays
        const running = await getRunningOverlays();
        setRunningOverlays(running);
      } else {
        toaster.toast({
          title: "Close Failed",
          body: result.error || "Unknown error"
        });
      }
    } catch (e) {
      console.error("Close error:", e);
      toaster.toast({
        title: "Error",
        body: "Failed to close overlay"
      });
    }
    setClosingApps(prev => {
      const next = new Set(prev);
      next.delete(appId);
      return next;
    });
  };

  const refreshFlatpaks = async () => {
    setIsLoading(true);
    try {
      const apps = await listInstalledFlatpaks();
      setFlatpaks(apps);
      toaster.toast({
        title: "Refreshed",
        body: `Found ${apps.length} flatpak apps`
      });
    } catch (e) {
      console.error("Refresh error:", e);
    }
    setIsLoading(false);
  };

  const handleRefreshWindows = async (appId: string) => {
    try {
      const result = await refreshOverlayWindows(appId);
      if (result.success) {
        if (result.new_windows && result.new_windows > 0) {
          toaster.toast({
            title: "Windows Refreshed",
            body: `Found ${result.new_windows} new window(s), total: ${result.total_windows}`
          });
        } else {
          toaster.toast({
            title: "Windows Refreshed",
            body: `No new windows found (${result.total_windows} total)`
          });
        }
        // Refresh running overlays to update window count
        const running = await getRunningOverlays();
        setRunningOverlays(running);
      } else {
        toaster.toast({
          title: "Refresh Failed",
          body: result.error || "Unknown error"
        });
      }
    } catch (e) {
      console.error("Refresh windows error:", e);
      toaster.toast({
        title: "Error",
        body: "Failed to refresh overlay windows"
      });
    }
  };

  if (isLoading) {
    return (
      <PanelSection title="Overlay Launcher">
        <PanelSectionRow>
          <div style={{ display: "flex", justifyContent: "center", padding: "20px" }}>
            <Spinner />
          </div>
        </PanelSectionRow>
      </PanelSection>
    );
  }

  if (!depsOk) {
    return (
      <PanelSection title="Overlay Launcher">
        <PanelSectionRow>
          <div style={{ color: "#ff6b6b", padding: "10px" }}>
            ⚠️ Missing required tools (flatpak, xdotool, or xprop). 
            Please install them to use this plugin.
          </div>
        </PanelSectionRow>
      </PanelSection>
    );
  }

  const dropdownOptions = flatpaks.map(app => ({
    data: app.app_id,
    label: app.name || app.app_id
  }));

  const isSelectedRunning = runningOverlays.some(o => o.app_id === selectedAppId);

  return (
    <>
      <PanelSection title="Launch Overlay">
        <PanelSectionRow>
          <Dropdown
            rgOptions={dropdownOptions}
            selectedOption={selectedAppId}
            onChange={(option) => setSelectedAppId(option.data)}
            strDefaultLabel="Select a Flatpak..."
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <ButtonItem
            layout="below"
            onClick={handleLaunch}
            disabled={!selectedAppId || isLaunching || isSelectedRunning}
          >
            <div style={{ display: "flex", alignItems: "center", justifyContent: "center", gap: "8px" }}>
              {isLaunching ? <Spinner width={16} height={16} /> : <FaRocket />}
              {isLaunching ? "Launching..." : isSelectedRunning ? "Already Running" : "Launch as Overlay"}
            </div>
          </ButtonItem>
        </PanelSectionRow>
        <PanelSectionRow>
          <ButtonItem
            layout="below"
            onClick={refreshFlatpaks}
            disabled={isLoading}
          >
            <div style={{ display: "flex", alignItems: "center", justifyContent: "center", gap: "8px" }}>
              <FaSync />
              Refresh App List
            </div>
          </ButtonItem>
        </PanelSectionRow>
      </PanelSection>

      {runningOverlays.length > 0 && (
        <PanelSection title="Running Overlays">
          {runningOverlays.map(overlay => {
            const appInfo = flatpaks.find(f => f.app_id === overlay.app_id);
            const displayName = appInfo?.name || overlay.app_id;
            const isClosing = closingApps.has(overlay.app_id);
            const windowCount = overlay.window_ids?.length || 0;
            
            return (
              <div key={overlay.app_id}>
                <PanelSectionRow>
                  <div style={{ 
                    display: "flex", 
                    alignItems: "center", 
                    justifyContent: "space-between",
                    width: "100%",
                    padding: "4px 0"
                  }}>
                    <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", flex: 1, fontSize: "14px" }}>
                      {displayName}
                    </span>
                    <span style={{ color: "#888", fontSize: "11px", marginLeft: "8px" }}>
                      {windowCount} win
                    </span>
                  </div>
                </PanelSectionRow>
                <PanelSectionRow>
                  <ButtonItem
                    layout="below"
                    onClick={() => handleRefreshWindows(overlay.app_id)}
                  >
                    <div style={{ display: "flex", alignItems: "center", justifyContent: "center", gap: "6px" }}>
                      <FaSync style={{ fontSize: "12px" }} />
                      Refresh Windows
                    </div>
                  </ButtonItem>
                </PanelSectionRow>
                <PanelSectionRow>
                  <ButtonItem
                    layout="below"
                    onClick={() => handleClose(overlay.app_id)}
                    disabled={isClosing}
                  >
                    <div style={{ display: "flex", alignItems: "center", justifyContent: "center", gap: "6px" }}>
                      {isClosing ? <Spinner width={14} height={14} /> : <FaTimes style={{ color: "#ff6b6b" }} />}
                      Close Overlay
                    </div>
                  </ButtonItem>
                </PanelSectionRow>
              </div>
            );
          })}
          <PanelSectionRow>
            <div style={{ color: "#888", fontSize: "11px", padding: "4px 0" }}>
              💡 If input isn't working, try "Refresh Windows"
            </div>
          </PanelSectionRow>
        </PanelSection>
      )}
    </>
  );
}

export default definePlugin(() => {
  console.log("Overlay Plugin initializing");

  return {
    // The name shown in various decky menus
    name: "Overlay Launcher",
    // The element displayed at the top of your plugin's menu
    titleView: <div className={staticClasses.Title}>Overlay Launcher</div>,
    // The content of your plugin's menu
    alwaysRender: true,
    content: <Content />,
    // The icon displayed in the plugin list
    icon: <FaLayerGroup />,
    // The function triggered when your plugin unloads
    onDismount() {
      console.log("Overlay Plugin unloading");
    },
  };
});
