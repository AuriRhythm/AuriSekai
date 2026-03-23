# AuriSekai

AuriSekai is an upcoming auto-play bot for the sekai game 

Currently, the project only has a working custom way to get device video output to imgui (Hijacking scrcpy-server). It completely avoid `scrcpy.exe`, connecting directly to the Android device's `scrcpy-server.jar` via TCP sockets. This allows higher latency than scrcpy(not really that high) (bcs sw decoding. Hw decoding is much slower because it need to go to cpu then gpu.) rendering directly inside a custom ImGui desktop interface.

## Current Features

* **Standalone Scrcpy Client:** Extracts raw H.264/H.265 NAL units and OPUS audio packets directly from the server sockets.
* **Couple ms higher than scrcpy but managable** Uses PyAV bcs not alot of people have nvidia gpu.
* **Zero-Copy GPU Color Conversion:** Bypasses CPU-heavy YUV-to-RGB conversion by uploading raw YUV planes directly to OpenGL and converting them on the GPU via a custom GLSL fragment shader. (this is a lie btw)
* **Custom ImGui Dashboard:** Integrated Dear ImGui interface via PyOpenGL and GLFW for seamless monitoring and control. (atleast for scrcpy menu)
* **Device Management:** Built-in ADB device scanning, tunnel forwarding, and resolution/FPS configuration. (atleast this is true.)

## Upcoming Features

* **AutoSekai:** Automated touch-injection pipeline based on the `scrcpy` binary control protocol. [This is complete but just need time to implement it here.]

* **Computer Vision:** Real-time AI chart text detection and note tracking. (its just windows ocr.)

* **.SUS Chart Parsing:** Loading and mapping custom chart files to automated actions. (Thanks Sekai.best!)

## 🛠️ Prerequisites & Installation

1. **Python 3.11** Only.
2. Install the required Python dependencies:bash
   pip install pyimgui[glfw] pyopengl glfw av numpy pyaudio pywin32
   ```
3. **External Dependencies:** This project requires `adb` and `scrcpy-server.jar` (Version 3.3.4) to function. The script will automatically download required files.
## 🎮 Usage

1. Connect your Android device to your PC via USB.
2. Ensure **USB Debugging** is enabled in your device's Developer Options.
3. Run the application:
   ```bash
   python main.py
   ```
4. Select your target device and resolution from the left panel, and click **Connect**.

## ⚠️ Disclaimer

This software is developed for educational and research purposes. Using automation tools, bots, or memory-reading software may violate the Terms of Service of the game. Use at your own risk.

## 📄 License

This project is released into the public domain under the **UNLICENSE**.

# Honorable mention
[Scrcpy](https://github.com/Genymobile/scrcpy) by GenyMobile - The existance of scrcpy helped this repository by A MILE.

[py-scrcpy-client](https://github.com/leng-yue/py-scrcpy-client) by leng yue - Code to get video output via tcp socket. (maybeidk but i got the idea from this repo)

[pyimgui](https://github.com/pyimgui/pyimgui) - I don't want to write shit in c

[Senyx](https://github.com/senyx) - At first, I thought I can only inject touches to scrcpy-server but even video too! how cool is that? (Discovered when implementing autosekai)

```
This project exist because I want to learn ImGUI. yeah. thats why the menu and scrcpy come first then the bot later.

Anyway- Alot of this readme part is created by AI (Gemini) for the meme ofcourse. My code? Deep Research by Gemini pro too. Why? Because i don't know openGL. (I know abit of vulkan but theres no opencv for it. fuck)
```
