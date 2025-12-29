import os
import subprocess
import time
from typing import Dict, List, Optional

# The decky plugin module is located at decky-loader/plugin
# For easy intellisense checkout the decky-loader code repo
# and add the `decky-loader/plugin/imports` path to `python.analysis.extraPaths` in `.vscode/settings.json`
import decky
import asyncio


def get_clean_env() -> dict:
    """Get a clean environment without Decky's bundled library paths.
    
    Decky Loader runs in a PyInstaller bundle with its own OpenSSL libraries
    that conflict with system tools like flatpak. We need to clear LD_LIBRARY_PATH
    so system commands use the system's native libraries.
    """
    env = os.environ.copy()
    # Remove library path overrides that cause OpenSSL version conflicts
    env.pop("LD_LIBRARY_PATH", None)
    env.pop("LD_PRELOAD", None)
    # Ensure DISPLAY is set for X11 tools (gamescope uses :0 or :1)
    if "DISPLAY" not in env:
        env["DISPLAY"] = ":0"
    return env


class Plugin:
    # Track running flatpak overlay processes: {app_id: {"process": Popen, "pid": int, "window_id": str, "launching": bool}}
    flatpak_processes: Dict[str, dict] = {}

    async def list_installed_flatpaks(self) -> List[dict]:
        """List all installed flatpak applications."""
        try:
            result = subprocess.run(
                ["flatpak", "list", "--app", "--columns=application,name"],
                capture_output=True,
                text=True,
                timeout=10,
                env=get_clean_env()
            )
            
            if result.returncode != 0:
                decky.logger.error(f"Failed to list flatpaks: {result.stderr}")
                return []
            
            apps = []
            for line in result.stdout.strip().split("\n"):
                if line:
                    parts = line.split("\t")
                    if len(parts) >= 2:
                        apps.append({
                            "app_id": parts[0].strip(),
                            "name": parts[1].strip()
                        })
                    elif len(parts) == 1:
                        apps.append({
                            "app_id": parts[0].strip(),
                            "name": parts[0].strip()
                        })
            
            decky.logger.info(f"Found {len(apps)} installed flatpaks")
            return apps
            
        except subprocess.TimeoutExpired:
            decky.logger.error("Timeout while listing flatpaks")
            return []
        except FileNotFoundError:
            decky.logger.error("flatpak command not found")
            return []
        except Exception as e:
            decky.logger.error(f"Error listing flatpaks: {e}")
            return []

    async def launch_flatpak_overlay(self, app_id: str) -> dict:
        """Launch a flatpak application as a gamescope overlay."""
        try:
            # Check if already running
            if app_id in self.flatpak_processes:
                return {
                    "success": False,
                    "error": f"{app_id} is already running as an overlay"
                }
            
            decky.logger.info(f"Launching flatpak overlay: {app_id}")
            
            # Launch the flatpak application
            proc = subprocess.Popen(
                ["flatpak", "run", app_id],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,  # Detach from parent process group
                env=get_clean_env()
            )
            
            # Store process info - mark as launching to prevent cleanup race
            self.flatpak_processes[app_id] = {
                "process": proc,
                "pid": proc.pid,
                "window_id": None,
                "launching": True
            }
            
            # Wait for window to spawn (run in executor to not block)
            await asyncio.get_event_loop().run_in_executor(
                None, lambda: time.sleep(2.0)
            )
            
            # Note: flatpak run is a wrapper that may exit while app continues running
            # So we don't check proc.poll() here - instead rely on window detection
            
            # Make the window float as an overlay
            window_id = await self._make_window_overlay(proc.pid, app_id)
            
            # Mark as no longer launching
            if app_id in self.flatpak_processes:
                self.flatpak_processes[app_id]["launching"] = False
            
            if window_id:
                if app_id in self.flatpak_processes:
                    self.flatpak_processes[app_id]["window_id"] = window_id
                decky.logger.info(f"Successfully launched {app_id} as overlay (window: {window_id})")
                return {
                    "success": True,
                    "app_id": app_id,
                    "pid": proc.pid,
                    "window_id": window_id
                }
            else:
                # Window property failed but process is running
                decky.logger.warning(f"Launched {app_id} but couldn't set overlay property")
                return {
                    "success": True,
                    "app_id": app_id,
                    "pid": proc.pid,
                    "window_id": None,
                    "warning": "Could not set GAMESCOPE_EXTERNAL_OVERLAY property"
                }
                
        except FileNotFoundError as e:
            decky.logger.error(f"Command not found: {e}")
            return {"success": False, "error": f"Command not found: {e.filename}"}
        except Exception as e:
            decky.logger.error(f"Error launching flatpak overlay: {e}")
            return {"success": False, "error": str(e)}

    async def _make_window_overlay(self, pid: int, app_id: str) -> Optional[str]:
        """Set GAMESCOPE_EXTERNAL_OVERLAY property on the window for the given app.
        
        Note: flatpak runs apps in a sandbox with a different PID, so we search
        by window name/class derived from the app_id instead of the wrapper PID.
        """
        try:
            # Extract app name from app_id (e.g., "app.xemu.xemu" -> "xemu")
            app_name = app_id.split(".")[-1]
            
            decky.logger.info(f"Searching for window matching app: {app_name} (pid hint: {pid})")
            
            # Retry loop - app windows can take time to appear
            window_id = None
            max_retries = 5
            
            for attempt in range(max_retries):
                if attempt > 0:
                    decky.logger.info(f"Retry {attempt}/{max_retries} searching for {app_name} window...")
                    await asyncio.get_event_loop().run_in_executor(
                        None, lambda: time.sleep(1.0)
                    )
                
                # Strategy 1: Search by class name (most reliable for flatpaks)
                result = subprocess.run(
                    ["xdotool", "search", "--class", app_name],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    env=get_clean_env()
                )
                
                if result.returncode == 0 and result.stdout.strip():
                    window_ids = result.stdout.strip().split("\n")
                    window_id = window_ids[-1]  # Get the most recent window
                    decky.logger.info(f"Found window by class: {window_id}")
                    break
                
                # Strategy 2: Search by name if class didn't work
                result = subprocess.run(
                    ["xdotool", "search", "--name", app_name],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    env=get_clean_env()
                )
                
                if result.returncode == 0 and result.stdout.strip():
                    window_ids = result.stdout.strip().split("\n")
                    window_id = window_ids[-1]
                    decky.logger.info(f"Found window by name: {window_id}")
                    break
                
                # Strategy 3: Try the original PID approach as fallback
                result = subprocess.run(
                    ["xdotool", "search", "--pid", str(pid)],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    env=get_clean_env()
                )
                
                if result.returncode == 0 and result.stdout.strip():
                    window_ids = result.stdout.strip().split("\n")
                    window_id = window_ids[0]
                    decky.logger.info(f"Found window by PID: {window_id}")
                    break
            
            if not window_id:
                decky.logger.warning(f"No window found for {app_name}")
                return None
            
            decky.logger.info(f"Setting GAMESCOPE_EXTERNAL_OVERLAY on window {window_id}")
            
            # Set the GAMESCOPE_EXTERNAL_OVERLAY property
            # This tells gamescope to render this window as an external overlay (zpos 2)
            xprop_result = subprocess.run(
                [
                    "xprop", "-id", window_id,
                    "-f", "GAMESCOPE_EXTERNAL_OVERLAY", "32c",
                    "-set", "GAMESCOPE_EXTERNAL_OVERLAY", "1"
                ],
                capture_output=True,
                text=True,
                timeout=5,
                env=get_clean_env()
            )
            
            if xprop_result.returncode != 0:
                decky.logger.error(f"Failed to set overlay property: {xprop_result.stderr}")
                return None
            
            decky.logger.info(f"Set GAMESCOPE_EXTERNAL_OVERLAY on window {window_id}")
            return window_id
            
        except subprocess.TimeoutExpired:
            decky.logger.error("Timeout while setting window overlay property")
            return None
        except FileNotFoundError as e:
            decky.logger.error(f"Required tool not found: {e.filename}")
            return None
        except Exception as e:
            decky.logger.error(f"Error making window overlay: {e}")
            return None

    async def close_flatpak_overlay(self, app_id: str) -> dict:
        """Close a running flatpak overlay."""
        try:
            if app_id not in self.flatpak_processes:
                return {"success": False, "error": f"{app_id} is not running"}
            
            proc_info = self.flatpak_processes[app_id]
            proc = proc_info["process"]
            
            decky.logger.info(f"Closing flatpak overlay: {app_id} (PID: {proc_info['pid']})")
            
            # Try graceful termination first
            proc.terminate()
            
            # Wait up to 3 seconds for graceful shutdown
            try:
                await asyncio.get_event_loop().run_in_executor(
                    None, lambda: proc.wait(timeout=3)
                )
            except subprocess.TimeoutExpired:
                # Force kill if still running
                decky.logger.warning(f"Force killing {app_id}")
                proc.kill()
                proc.wait()
            
            del self.flatpak_processes[app_id]
            decky.logger.info(f"Closed {app_id}")
            
            return {"success": True, "app_id": app_id}
            
        except Exception as e:
            decky.logger.error(f"Error closing flatpak overlay: {e}")
            # Clean up tracking even if there was an error
            if app_id in self.flatpak_processes:
                del self.flatpak_processes[app_id]
            return {"success": False, "error": str(e)}

    async def get_running_overlays(self) -> List[dict]:
        """Get list of currently running overlay applications."""
        running = []
        to_remove = []
        
        for app_id, proc_info in self.flatpak_processes.items():
            # Skip apps that are still launching (to avoid race conditions)
            if proc_info.get("launching", False):
                running.append({
                    "app_id": app_id,
                    "pid": proc_info["pid"],
                    "window_id": proc_info["window_id"]
                })
                continue
                
            proc = proc_info["process"]
            # Check if still running (only for non-launching apps)
            # Note: flatpak wrapper may have exited but app runs - check window instead
            if proc_info.get("window_id"):
                # Has a window, consider it running
                running.append({
                    "app_id": app_id,
                    "pid": proc_info["pid"],
                    "window_id": proc_info["window_id"]
                })
            elif proc.poll() is None:
                # Process still running
                running.append({
                    "app_id": app_id,
                    "pid": proc_info["pid"],
                    "window_id": proc_info["window_id"]
                })
            else:
                to_remove.append(app_id)
        
        # Clean up dead processes
        for app_id in to_remove:
            del self.flatpak_processes[app_id]
            decky.logger.info(f"Cleaned up terminated process: {app_id}")
        
        return running

    async def check_dependencies(self) -> dict:
        """Check if required system tools are available."""
        deps = {
            "flatpak": False,
            "xdotool": False,
            "xprop": False
        }
        
        for tool in deps.keys():
            try:
                result = subprocess.run(
                    ["which", tool],
                    capture_output=True,
                    timeout=2,
                    env=get_clean_env()
                )
                deps[tool] = result.returncode == 0
            except Exception:
                deps[tool] = False
        
        all_ok = all(deps.values())
        return {"success": all_ok, "dependencies": deps}

    # Asyncio-compatible long-running code, executed in a task when the plugin is loaded
    async def _main(self):
        self.loop = asyncio.get_event_loop()
        self.flatpak_processes = {}
        
        decky.logger.info("Overlay Plugin initialized")
        
        # Check dependencies on startup
        deps = await self.check_dependencies()
        if not deps["success"]:
            missing = [k for k, v in deps["dependencies"].items() if not v]
            decky.logger.warning(f"Missing dependencies: {missing}")
        else:
            decky.logger.info("All dependencies available")

    # Function called first during the unload process
    async def _unload(self):
        decky.logger.info("Unloading Overlay Plugin")
        
        # Close all running overlays
        for app_id in list(self.flatpak_processes.keys()):
            try:
                await self.close_flatpak_overlay(app_id)
            except Exception as e:
                decky.logger.error(f"Error closing {app_id} during unload: {e}")
        
        decky.logger.info("Overlay Plugin unloaded")

    # Function called after `_unload` during uninstall
    async def _uninstall(self):
        decky.logger.info("Uninstalling Overlay Plugin")

    # Migrations that should be performed before entering `_main()`.
    async def _migration(self):
        decky.logger.info("Migrating")
        # Migrate settings if needed
        decky.migrate_settings(
            os.path.join(decky.DECKY_HOME, "settings", "overlay-plugin.json"),
            os.path.join(decky.DECKY_USER_HOME, ".config", "overlay-plugin"))
