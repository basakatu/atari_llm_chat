# Atari Chatbot & Ollama Bridge

## Description

This project connects vintage **Atari 8-bit computers** to modern artificial intelligence models running locally via **Ollama**. It consists of two primary components working in tandem:

1. **Atari BASIC Client (`chat14_.txt`)**: A feature-rich terminal application written in Atari BASIC for 8-bit Atari computers. It manages serial communication over the `R1:` handler (via the Atari 850 interface / R-Device), renders a custom hardware-accelerated status bar using Display List Interrupts (DLI), formats ATASCII text, and provides real-time status indicators (connection status, RTC clock, animated thinking dots, software cursor blinking, and dynamic color customization).

2. **Python Bridge Server (`atari_bridge.txt`)**: A local TCP socket server (`127.0.0.1:8080`) that bridges the Atari terminal with an Ollama LLM endpoint. The bridge handles the technical constraints of vintage 8-bit hardware by:
   - Formatting text to fit the 37-column Atari text mode (`SCREEN_WIDTH = 37`), stripping unsupported Markdown/Unicode formatting, and converting Polish diacritics to standard ASCII.
   - Pacing data transmission (`CHARS_PER_SEC = 100`, adjustable via `/TYPESPEED`) to prevent serial buffer overruns on the Atari R-Device handler.
   - Injecting real-time context dynamically into prompt templates (local time/date, live weather forecasts via Open-Meteo API).
   - Managing asynchronous notifications (timed `/REMIND` alarms delivered with ATASCII buzzer beeps).
   - Initiating autonomous conversation prompts when the user remains idle (`IDLE_MINUTES = 20`).

### Key Bridge Configuration Parameters

```python
OLLAMA_URL     = "http://localhost:11434/api/chat"
MODEL_NAME     = "qwen2.5-coder:14b"  # Local LLM model name
HOST           = "127.0.0.1"          # Listening IP address
PORT           = 8080                 # Listening TCP port
BOT_NAME       = "Wendy"              # Assistant prompt label
SCREEN_WIDTH   = 37                   # Max characters per line (Atari GR.0 mode)
CHARS_PER_SEC = 100                  # Transmission pace (~1200 baud equivalent)
IDLE_MINUTES   = 20                   # Silence duration before autonomous initiative
SYSTEM_PROMPT  = (
    "You are Wendy Jenkins, a highly creative, and immersive companion. "
    "The user talks to you through a tiny 38-column retro terminal. "
    "Keep replies short (2-4 sentences), plain text only: no markdown, no emoji, no lists."
)
```

---

## Commands

### Client Commands (Atari BASIC Client)

| Command | Syntax | Description |
| :--- | :--- | :--- |
| **/BARCOLOR** | `/BARCOLOR <name>`<br>`/BARCOLOR <h,f,l>` | Sets the color scheme of the top status bar (driven by DLI). Accepts a preset color name or three numeric values (`0-15`) for **H**ue, **F**ill Luminance, and **L**etter Luminance. |
| **/BLINK** | `/BLINK` | Toggles the software-controlled cursor blinking effect on and off (`BLINKING CURSOR ON` / `OFF`). |
| **/CLICK** | `/CLICK` | Toggles keyboard keypress click audio feedback on and off by modifying OS register 731 (`POKE 731, 255` for OFF, `POKE 731, 0` for ON). |
| **/CLS** | `/CLS` | Clears the terminal screen (`CHR$(125)`), repositions the cursor to row 1, column 2, and redraws the top status bar header. |
| **/COLOR** | `/COLOR <name>`<br>`/COLOR <h,b,l>` | Sets the main terminal screen colors (background, border, and text). Accepts a preset color name or three numeric values (`0-15`) representing **H**ue, **B**ackground Luminance, and **L**etter Luminance. |
| **/EXIT** | `/EXIT` | Gracefully disconnects from the chat server. Sends the Hayes modem hangup sequence (`+++` / `ATH`), closes serial `R1:` and keyboard channels, restores standard `GRAPHICS 0` screen settings, and terminates execution. |
| **/NICK** | `/NICK <name>` | Sets or updates the user's nickname (up to 12 characters). Updates the local inverse prompt on the Atari screen and notifies the bridge server. |

### Server / Bridge Commands (Python Server)

| Command | Syntax | Description |
| :--- | :--- | :--- |
| **/REMIND** | `/REMIND <minutes> <text>`<br>`/REMIND <HH:MM> <text>`<br>`/REMIND clear` | Sets a timed reminder. When triggered, the bridge sends an asynchronous notification with audio beeps (`BELL`) to the Atari terminal. Use `/REMIND clear` to remove all pending reminders. |
| **/RESET** | `/RESET` | Clears the LLM conversation context history on the bridge server, starting a fresh chat session. |
| **/TYPESPEED** | `/TYPESPEED <slow\|normal\|fast\|10-100>` | Adjusts the text streaming rate (characters per second) from the bridge server to the Atari terminal to prevent serial buffer overflow on 8-bit hardware. |

### Color Presets

The following color preset names can be used with both `/COLOR` and `/BARCOLOR`:

`BLACK`, `ORANGE`, `SALMON`, `PINK`, `PURPLE`, `VIOLET`, `NAVY`, `BLUE`, `SKY`, `TURQUOISE`, `EMERALD`, `GREEN`, `GRASS`, `OLIVE`, `BROWN`, `AUTUMN`.

---

## License

**MIT License**

Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated documentation files, to deal in the Software without restriction, including without limitation the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software.

*Note: Citation or attribution is not required, but appreciated!*
