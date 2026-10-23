# Architektur

## Überblick

Zwei getrennte Wege laufen über denselben Raspberry Pi:

1. **Auftragsweg** – ChatGPT → RDC-Connector → Pi-Agent → Queue → Firmen-PC → Kilo Code.
2. **Weckweg** – beliebiger HTTPS-Client → Caddy → Wake-Dienst → WireGuard → Firmen-LAN-Relay → Magic Packet.

```mermaid
flowchart LR
    subgraph Cloud
        GPT[ChatGPT<br/>Go + RDC-App]
        RDC[mcp.desktopcommander.app<br/>MCP-Relay]
    end

    subgraph Pi["Raspberry Pi (raspberrypi2, 192.168.178.30)"]
        CADDY[Caddy :443<br/>Let's Encrypt]
        RDCAG[RDC-Agent<br/>User rdc]
        SUBMIT[kilo-submit]
        BRIDGE[Kilo-Bridge :8892<br/>SQLite-Queue]
        WAKE[Wake-Dienst :8891<br/>User waker]
        WG[wg-firma<br/>WireGuard]
    end

    subgraph Firma["Firmen-LAN"]
        RELAY[Intranet-WoL-Relay<br/>192.168.10.146]
        PC[A1459<br/>192.168.50.56]
        SCHL[ai.schleissheimer.de<br/>LLM-API]
    end

    GPT --> RDC --> RDCAG --> SUBMIT --> BRIDGE
    CADDY --> BRIDGE
    CADDY --> WAKE
    WAKE --> WG --> RELAY --> PC
    BRIDGE -. SSE/HTTPS .-> PC
```

## Datenfluss eines Auftrags

```mermaid
sequenceDiagram
    autonumber
    participant U as Benutzer (Sprache)
    participant GPT as ChatGPT
    participant RDC as RDC-Relay
    participant PI as Pi-Agent (rdc)
    participant BR as Kilo-Bridge
    participant AG as Client (A1459)
    participant WEB as KiloCodeWeb
    participant CLI as kilo.exe

    U->>GPT: "Lege Datei X an"
    GPT->>RDC: MCP-Tool (start_process)
    RDC->>PI: Befehl kilo-submit "…"
    PI->>BR: POST /jobs (Submit-Token)
    BR-->>PI: 202 {id}
    BR-->>AG: SSE job-Event (Agent-Token)
    AG->>BR: POST /jobs/{id}/ack
    AG->>WEB: POST /api/run
    WEB->>CLI: kilo run --auto --model … --dir …
    CLI->>CLI: Tool-Call (write/edit/bash)
    CLI-->>WEB: JSON-Events + Ergebnis
    WEB-->>AG: Status "Fertig"
    AG->>BR: POST /jobs/{id}/complete
    BR-->>AG: 200 done
```

## Deployment

```mermaid
flowchart TB
    subgraph Internet
        C[Caddy :443<br/>holger80.dynv6.net]
    end

    subgraph Pi["Pi 4 · Debian 13 · aarch64"]
        direction TB
        S1[wake.service<br/>waker · 127.0.0.1:8891]
        S2[kilo-bridge.service<br/>bridge · 127.0.0.1:8892]
        S3[rdc-agent.service<br/>rdc · Node 22]
        D1[(wake/config.env<br/>root:waker 0640)]
        D2[(kilo-bridge/config.env<br/>root:bridge 0640)]
        D3[(queue.db<br/>bridge 0750)]
        D4[(/etc/wireguard/wg-firma.conf<br/>root 0600)]
    end

    subgraph PC["A1459 · Windows 11"]
        T1[Task KiloAgent<br/>Interactive]
        T2[Task KiloCodeWeb<br/>Interactive]
        F1[%LOCALAPPDATA%\KiloAgent<br/>config.json ACL]
        F2[~/.config/kilo/kilo.jsonc<br/>Provider + Key]
        F3[C:\Users\dahinten\kilo-work<br/>Workspace + Permissions]
    end

    C -->|/wake| S1
    C -->|/jobs*| S2
    S1 --> D1
    S1 --> D4
    S2 --> D2
    S2 --> D3
    T1 --> F1
    T2 --> F2
    T2 --> F3
    T1 -. SSE über HTTPS .-> C
    S3 -. MCP .-> D1
```

## Zustandsmodell der Queue

```mermaid
stateDiagram-v2
    [*] --> pending: POST /jobs
    pending --> leased: SSE-Zustellung (Lease 120 s)
    leased --> running: POST /ack (Lease 3600 s)
    leased --> leased: Lease abgelaufen → erneut zustellen
    running --> done: POST /complete status=done
    running --> failed: POST /complete status=failed
    running --> failed: Running-Timeout (3600 s)
    leased --> failed: 5 Versuche überschritten
    done --> [*]
    failed --> [*]
```

## Netzwerkpfade

| Pfad | Protokoll | Anmerkung |
|---|---|---|
| ChatGPT → `mcp.desktopcommander.app` | HTTPS/MCP | ausgehend vom Pi-Agenten; Drittanbieter-Relay |
| Client A1459 → Pi | HTTPS + SSE | ausgehend, Port 443, kein Inbound im Firmennetz |
| Pi → `ai.schleissheimer.de` | HTTPS | nur lokal auf A1459 (CLI), nicht vom Pi |
| Pi → `192.168.10.146` | HTTP über `wg-firma` | WoL-Relay im Intranet |
| Pi → `192.168.50.56` | TCP 3389 über `wg-firma` | nur Erreichbarkeitsprüfung |

Der Pi baut `wg-firma` ausschließlich für den Wake-Vorgang auf und trennt die
Verbindung wieder, wenn er sie selbst gestartet hat. Der Auftragsweg braucht
kein VPN auf dem Pi.
