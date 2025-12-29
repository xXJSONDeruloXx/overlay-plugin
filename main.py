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
                "window_ids": [],  # Track ALL window IDs
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
                    self.flatpak_processes[app_id]["window_ids"].append(window_id)
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
        """Set gamescope overlay properties on ALL windows for the given app.
        
        This implements input focus handling similar to HHD (Handheld Daemon).
        Key properties:
        - GAMESCOPE_EXTERNAL_OVERLAY: Makes window render above game (zpos 2)
        - STEAM_OVERLAY: Tells Steam this is an overlay
        - STEAM_INPUT_FOCUS: Routes gamepad/keyboard input to this window
        - STEAM_TOUCH_CLICK_MODE: Routes touch/mouse input (set on root window)
        - GAMESCOPE_NO_FOCUS: Prevents gamescope from treating as focusable game
        
        Note: flatpak runs apps in a sandbox with a different PID, so we search
        by window name/class derived from the app_id instead of the wrapper PID.
        
        IMPORTANT: Many apps create multiple windows. We must set properties on ALL
        of them to prevent input leaking through unhandled windows.
        """
        try:
            # Extract app name from app_id (e.g., "com.github.Matoking.protontricks" -> "protontricks")
            app_name = app_id.split(".")[-1]
            
            decky.logger.info(f"Searching for windows matching app: {app_name} (pid hint: {pid})")
            
            # Retry loop - app windows can take time to appear
            all_window_ids = set()
            max_retries = 5
            
            for attempt in range(max_retries):
                if attempt > 0:
                    decky.logger.info(f"Retry {attempt}/{max_retries} searching for {app_name} windows...")
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
                    all_window_ids.update(window_ids)
                    decky.logger.info(f"Found {len(window_ids)} windows by class: {window_ids}")
                
                # Strategy 2: Also search by name to catch any we missed
                result = subprocess.run(
                    ["xdotool", "search", "--name", app_name],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    env=get_clean_env()
                )
                
                if result.returncode == 0 and result.stdout.strip():
                    window_ids = result.stdout.strip().split("\n")
                    new_windows = set(window_ids) - all_window_ids
                    if new_windows:
                        decky.logger.info(f"Found {len(new_windows)} additional windows by name: {list(new_windows)}")
                    all_window_ids.update(window_ids)
                
                # Strategy 3: Also try PID approach for any windows we might have missed
                result = subprocess.run(
                    ["xdotool", "search", "--pid", str(pid)],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    env=get_clean_env()
                )
                
                if result.returncode == 0 and result.stdout.strip():
                    window_ids = result.stdout.strip().split("\n")
                    new_windows = set(window_ids) - all_window_ids
                    if new_windows:
                        decky.logger.info(f"Found {len(new_windows)} additional windows by PID: {list(new_windows)}")
                    all_window_ids.update(window_ids)
                
                # If we found windows, continue but check for more on next iteration
                if all_window_ids and attempt >= 1:
                    break
            
            if not all_window_ids:
                decky.logger.warning(f"No windows found for {app_name}")
                return None
            
            decky.logger.info(f"Found {len(all_window_ids)} total windows for {app_name}: {list(all_window_ids)}")
            
            # Get the root window ID for setting global properties
            root_id = await self._get_root_window_id()
            
            # Set overlay properties on ALL windows
            primary_window_id = None
            for window_id in all_window_ids:
                decky.logger.info(f"Setting overlay properties on window {window_id}")
                success = await self._set_window_overlay_properties(window_id)
                if success and primary_window_id is None:
                    primary_window_id = window_id
            
            # Set STEAM_TOUCH_CLICK_MODE on the ROOT window to redirect touch/mouse input
            # Value 4 is used by HHD to route touch to the overlay
            # This is CRITICAL for touch and mouse input to reach the overlay
            if root_id:
                subprocess.run(
                    [
                        "xprop", "-id", root_id,
                        "-f", "STEAM_TOUCH_CLICK_MODE", "32c",
                        "-set", "STEAM_TOUCH_CLICK_MODE", "4"
                    ],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    env=get_clean_env()
                )
                decky.logger.info(f"Set STEAM_TOUCH_CLICK_MODE=4 on root window {root_id}")
            else:
                decky.logger.warning("Could not find root window for STEAM_TOUCH_CLICK_MODE")
            
            # Find and disable Steam's input focus so input goes to our overlay
            await self._disable_steam_input_focus()
            
            # Focus the primary window to ensure it receives input
            if primary_window_id:
                focus_result = subprocess.run(
                    ["xdotool", "windowactivate", "--sync", primary_window_id],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    env=get_clean_env()
                )
                
                if focus_result.returncode == 0:
                    decky.logger.info(f"Focused window {primary_window_id}")
                else:
                    decky.logger.warning(f"Could not focus window: {focus_result.stderr}")
            
            return primary_window_id
            
        except subprocess.TimeoutExpired:
            decky.logger.error("Timeout while setting window overlay property")
            return None
        except FileNotFoundError as e:
            decky.logger.error(f"Required tool not found: {e.filename}")
            return None
        except Exception as e:
            decky.logger.error(f"Error making window overlay: {e}")
            return None

    async def _get_root_window_id(self) -> Optional[str]:
        """Get the X11 root window ID."""
        try:
            result = subprocess.run(
                ["xwininfo", "-root", "-int"],
                capture_output=True,
                text=True,
                timeout=5,
                env=get_clean_env()
            )
            for line in result.stdout.split("\n"):
                if "Window id:" in line:
                    return line.split(":")[-1].strip().split()[0]
        except Exception as e:
            decky.logger.error(f"Error getting root window: {e}")
        return None

    async def _set_window_overlay_properties(self, window_id: str) -> bool:
        """Set all required overlay properties on a single window."""
        try:
            # Set GAMESCOPE_EXTERNAL_OVERLAY - renders above game (zpos 2)
            result = subprocess.run(
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
            if result.returncode != 0:
                decky.logger.error(f"Failed to set GAMESCOPE_EXTERNAL_OVERLAY on {window_id}: {result.stderr}")
                return False
            
            # Set STEAM_OVERLAY - indicates this is an overlay
            subprocess.run(
                [
                    "xprop", "-id", window_id,
                    "-f", "STEAM_OVERLAY", "32c",
                    "-set", "STEAM_OVERLAY", "1"
                ],
                capture_output=True,
                text=True,
                timeout=5,
                env=get_clean_env()
            )
            
            # Set STEAM_INPUT_FOCUS - routes gamepad/keyboard input
            subprocess.run(
                [
                    "xprop", "-id", window_id,
                    "-f", "STEAM_INPUT_FOCUS", "32c",
                    "-set", "STEAM_INPUT_FOCUS", "1"
                ],
                capture_output=True,
                text=True,
                timeout=5,
                env=get_clean_env()
            )
            
            # Set GAMESCOPE_NO_FOCUS - prevents gamescope from treating as focusable game
            subprocess.run(
                [
                    "xprop", "-id", window_id,
                    "-f", "GAMESCOPE_NO_FOCUS", "32c",
                    "-set", "GAMESCOPE_NO_FOCUS", "1"
                ],
                capture_output=True,
                text=True,
                timeout=5,
                env=get_clean_env()
            )
            
            decky.logger.info(f"Set all overlay properties on window {window_id}")
            return True
            
        except Exception as e:
            decky.logger.error(f"Error setting overlay properties on {window_id}: {e}")
            return False

    async def _disable_steam_input_focus(self):
        """Disable Steam's input focus so our overlay receives input."""
        try:
            steam_result = subprocess.run(
                ["xdotool", "search", "--class", "steamwebhelper"],
                capture_output=True,
                text=True,
                timeout=5,
                env=get_clean_env()
            )
            if steam_result.returncode == 0 and steam_result.stdout.strip():
                steam_windows = steam_result.stdout.strip().split("\n")
                for steam_win in steam_windows:
                    subprocess.run(
                        [
                            "xprop", "-id", steam_win,
                            "-f", "STEAM_INPUT_FOCUS", "32c",
                            "-set", "STEAM_INPUT_FOCUS", "0"
                        ],
                        capture_output=True,
                        text=True,
                        timeout=2,
                        env=get_clean_env()
                    )
                decky.logger.info(f"Disabled STEAM_INPUT_FOCUS on {len(steam_windows)} Steam windows")
        except Exception as e:
            decky.logger.error(f"Error disabling Steam input focus: {e}")

    async def refresh_overlay_windows(self, app_id: str) -> dict:
        """Refresh overlay properties on all windows for a running app.
        
        Call this if new windows appear (dialogs, etc.) that aren't receiving input.
        This will find all windows for the app and re-apply overlay properties.
        """
        try:
            if app_id not in self.flatpak_processes:
                return {"success": False, "error": f"{app_id} is not running"}
            
            proc_info = self.flatpak_processes[app_id]
            app_name = app_id.split(".")[-1]
            
            decky.logger.info(f"Refreshing overlay windows for {app_id}")
            
            # Find all windows for this app
            all_window_ids = set()
            
            # Search by class name
            result = subprocess.run(
                ["xdotool", "search", "--class", app_name],
                capture_output=True,
                text=True,
                timeout=5,
                env=get_clean_env()
            )
            if result.returncode == 0 and result.stdout.strip():
                all_window_ids.update(result.stdout.strip().split("\n"))
            
            # Search by name
            result = subprocess.run(
                ["xdotool", "search", "--name", app_name],
                capture_output=True,
                text=True,
                timeout=5,
                env=get_clean_env()
            )
            if result.returncode == 0 and result.stdout.strip():
                all_window_ids.update(result.stdout.strip().split("\n"))
            
            if not all_window_ids:
                return {"success": False, "error": f"No windows found for {app_id}"}
            
            # Find new windows we haven't configured yet
            existing_windows = set(proc_info.get("window_ids", []))
            new_windows = all_window_ids - existing_windows
            
            if new_windows:
                decky.logger.info(f"Found {len(new_windows)} new windows for {app_id}: {list(new_windows)}")
                
                # Apply overlay properties to new windows
                for window_id in new_windows:
                    await self._set_window_overlay_properties(window_id)
                
                # Update tracked windows
                proc_info["window_ids"] = list(all_window_ids)
                
                # Re-disable Steam input focus
                await self._disable_steam_input_focus()
                
                # Re-set touch click mode on root
                root_id = await self._get_root_window_id()
                if root_id:
                    subprocess.run(
                        [
                            "xprop", "-id", root_id,
                            "-f", "STEAM_TOUCH_CLICK_MODE", "32c",
                            "-set", "STEAM_TOUCH_CLICK_MODE", "4"
                        ],
                        capture_output=True,
                        text=True,
                        timeout=5,
                        env=get_clean_env()
                    )
            
            return {
                "success": True,
                "app_id": app_id,
                "total_windows": len(all_window_ids),
                "new_windows": len(new_windows)
            }
            
        except Exception as e:
            decky.logger.error(f"Error refreshing overlay windows: {e}")
            return {"success": False, "error": str(e)}

    async def close_flatpak_overlay(self, app_id: str) -> dict:
        """Close a running flatpak overlay and restore Steam input focus."""
        try:
            if app_id not in self.flatpak_processes:
                return {"success": False, "error": f"{app_id} is not running"}
            
            proc_info = self.flatpak_processes[app_id]
            proc = proc_info["process"]
            
            decky.logger.info(f"Closing flatpak overlay: {app_id} (PID: {proc_info['pid']})")
            
            # Restore Steam input focus before closing
            await self._restore_steam_input_focus()
            
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

    async def _restore_steam_input_focus(self):
        """Restore Steam's input focus after closing an overlay.
        
        This resets the input routing so Steam/games receive input again.
        """
        try:
            # Reset STEAM_TOUCH_CLICK_MODE on root window
            # Get root window
            root_result = subprocess.run(
                ["xwininfo", "-root", "-int"],
                capture_output=True,
                text=True,
                timeout=5,
                env=get_clean_env()
            )
            root_id = None
            for line in root_result.stdout.split("\n"):
                if "Window id:" in line:
                    root_id = line.split(":")[-1].strip().split()[0]
                    break
            
            if root_id:
                # Reset touch click mode to default (0)
                subprocess.run(
                    [
                        "xprop", "-id", root_id,
                        "-f", "STEAM_TOUCH_CLICK_MODE", "32c",
                        "-set", "STEAM_TOUCH_CLICK_MODE", "0"
                    ],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    env=get_clean_env()
                )
                decky.logger.info(f"Reset STEAM_TOUCH_CLICK_MODE on root window")
            
            # Restore Steam's input focus
            steam_result = subprocess.run(
                ["xdotool", "search", "--class", "steamwebhelper"],
                capture_output=True,
                text=True,
                timeout=5,
                env=get_clean_env()
            )
            if steam_result.returncode == 0 and steam_result.stdout.strip():
                steam_windows = steam_result.stdout.strip().split("\n")
                for steam_win in steam_windows:
                    subprocess.run(
                        [
                            "xprop", "-id", steam_win,
                            "-f", "STEAM_INPUT_FOCUS", "32c",
                            "-set", "STEAM_INPUT_FOCUS", "1"
                        ],
                        capture_output=True,
                        text=True,
                        timeout=2,
                        env=get_clean_env()
                    )
                    subprocess.run(
                        [
                            "xprop", "-id", steam_win,
                            "-f", "STEAM_OVERLAY", "32c",
                            "-set", "STEAM_OVERLAY", "1"
                        ],
                        capture_output=True,
                        text=True,
                        timeout=2,
                        env=get_clean_env()
                    )
                decky.logger.info(f"Restored STEAM_INPUT_FOCUS on Steam windows")
        except Exception as e:
            decky.logger.error(f"Error restoring Steam input focus: {e}")

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
                    "window_ids": proc_info.get("window_ids", [])
                })
                continue
                
            proc = proc_info["process"]
            # Check if still running (only for non-launching apps)
            # Note: flatpak wrapper may have exited but app runs - check window instead
            window_ids = proc_info.get("window_ids", [])
            if window_ids:
                # Has windows, consider it running
                running.append({
                    "app_id": app_id,
                    "pid": proc_info["pid"],
                    "window_ids": window_ids
                })
            elif proc.poll() is None:
                # Process still running
                running.append({
                    "app_id": app_id,
                    "pid": proc_info["pid"],
                    "window_ids": window_ids
                })
            else:
                to_remove.append(app_id)
        
        # Clean up dead processes
        for app_id in to_remove:
            del self.flatpak_processes[app_id]
            decky.logger.info(f"Cleaned up terminated process: {app_id}")
        
        return running
        
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
