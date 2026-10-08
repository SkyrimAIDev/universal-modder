package dev.rehan.passthrough.client;

import com.google.gson.JsonArray;
import com.google.gson.JsonObject;
import com.google.gson.JsonParser;
import dev.rehan.passthrough.MobWar;
import dev.rehan.passthrough.Nether;
import dev.rehan.passthrough.Passthrough;
import dev.rehan.passthrough.WorldBridge;
import java.io.IOException;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.security.MessageDigest;
import java.security.SecureRandom;
import java.util.Locale;
import net.minecraft.client.Minecraft;
import org.java_websocket.WebSocket;
import org.java_websocket.drafts.Draft;
import org.java_websocket.exceptions.InvalidDataException;
import org.java_websocket.framing.CloseFrame;
import org.java_websocket.handshake.ClientHandshake;
import org.java_websocket.handshake.ServerHandshakeBuilder;
import org.java_websocket.server.WebSocketServer;

/**
 * The host's connection: a WebSocket server on 127.0.0.1 (port 25599, or -Dpassthrough.port).
 *
 * <p>Loopback is not a boundary by itself: every local process can reach this port, and so can any web page
 * the player happens to have open - a browser may open a WebSocket to 127.0.0.1 from any origin and no CORS
 * check stands in the way. Since {@code {"t":"cmd"}} runs server commands as an operator, the handshake is
 * gated: a request carrying an {@code Origin} header is refused (browsers always send one, host scripts
 * never do), and every client must present the per-run token from {@code <PASSTHROUGH_WIN_DIR>/passthrough.token}
 * as {@code ws://127.0.0.1:25599/?token=...}.
 *
 * <p>Host to Minecraft (JSON, Minecraft coordinates):
 * <ul>
 * <li>{"t":"cam","f":frame,"p":[x,y,z],"r":[yaw,pitch,roll],"fov":vertical degrees,"fp":first person,"pl":[feet x,y,z],"h":body yaw}</li>
 * <li>{"t":"ground","c":[x,z,yBottom,yTop, ...]}: solid columns (barriers)</li>
 * <li>{"t":"clear"}: remove the barriers placed so far</li>
 * <li>{"t":"cmd","c":"time set noon"}: a server command</li>
 * <li>{"t":"key","k":"use|attack|pick|inventory|drop|swap|escape","down":bool}</li>
 * <li>{"t":"slot","n":0-8}, {"t":"scroll","d":+-1}, {"t":"hud","hidden":bool}, {"t":"view","w":px,"h":px}</li>
 * </ul>
 * Minecraft to host: {"t":"hello",...} on connect, {"t":"explosion","pos":[x,y,z],"r":radius}.
 * Relayed unchanged to the other clients: {"t":"gta",...} (director commands for the host plugin), {"t":"gtastate",...}.
 */
public final class HostLink extends WebSocketServer {
	private static final String TOKEN_FILE = "passthrough.token";
	private static HostLink instance;
	private final String token;

	private HostLink(final int port) {
		super(new InetSocketAddress("127.0.0.1", port));
		this.token = writeToken();
		this.setReuseAddr(true);
		this.setDaemon(true);
	}

	/** The folder every side of the link already shares (PASSTHROUGH_WIN_DIR, or -Dpassthrough.dir). */
	static Path linkDir() {
		String dir = System.getProperty("passthrough.dir", System.getenv("PASSTHROUGH_WIN_DIR"));
		return Paths.get(dir == null || dir.isBlank() ? "C:\\dev\\passthrough" : dir);
	}

	/** A fresh token each run, left in a file for the host scripts to read. */
	private static String writeToken() {
		byte[] raw = new byte[16];
		new SecureRandom().nextBytes(raw);
		StringBuilder hex = new StringBuilder(raw.length * 2);
		for (byte b : raw) {
			hex.append(String.format(Locale.ROOT, "%02x", b));
		}
		String tok = hex.toString();
		Path file = linkDir().resolve(TOKEN_FILE);
		try {
			Files.createDirectories(file.getParent());
			Files.writeString(file, tok + System.lineSeparator());
			Passthrough.LOG.info("host link token written to {}", file);
		} catch (IOException e) {
			Passthrough.LOG.error("could not write the host link token to {} ({}): nothing will be able to connect",
				file, e.toString());
		}
		return tok;
	}

	/** The token out of "/?token=...&..."; it is hex, so there is nothing to URL-decode. */
	private static String queryToken(final String resource) {
		int q = resource == null ? -1 : resource.indexOf('?');
		if (q < 0) {
			return "";
		}

		for (String pair : resource.substring(q + 1).split("&")) {
			if (pair.startsWith("token=")) {
				return pair.substring("token=".length());
			}
		}

		return "";
	}

	@Override
	public ServerHandshakeBuilder onWebsocketHandshakeReceivedAsServer(final WebSocket conn, final Draft draft,
			final ClientHandshake request) throws InvalidDataException {
		if (request.hasFieldValue("Origin")) {
			Passthrough.LOG.warn("refused a host link handshake from a browser (Origin: {})", request.getFieldValue("Origin"));
			throw new InvalidDataException(CloseFrame.POLICY_VALIDATION, "passthrough is not driven from a browser");
		}

		byte[] want = this.token.getBytes(StandardCharsets.UTF_8);
		byte[] got = queryToken(request.getResourceDescriptor()).getBytes(StandardCharsets.UTF_8);
		if (!MessageDigest.isEqual(want, got)) {
			Passthrough.LOG.warn("refused a host link handshake with a wrong or missing ?token=");
			throw new InvalidDataException(CloseFrame.POLICY_VALIDATION, "wrong or missing ?token=");
		}

		return super.onWebsocketHandshakeReceivedAsServer(conn, draft, request);
	}

	static void launch() {
		int port = Integer.getInteger("passthrough.port", 25599);
		instance = new HostLink(port);
		instance.start();
		Passthrough.events = message -> instance.broadcast(message);
	}

	@Override
	public void onStart() {
		Passthrough.LOG.info("host link listening on 127.0.0.1:{}", this.getPort());
	}

	@Override
	public void onOpen(final WebSocket conn, final ClientHandshake handshake) {
		Passthrough.LOG.info("host connected from {}", conn.getRemoteSocketAddress());
		conn.send(String.format(Locale.ROOT, "{\"t\":\"hello\",\"v\":1,\"shm\":\"%s\",\"pid\":%d}", FrameExporter.NAME.replace("\\", "\\\\"), ProcessHandle.current().pid()));
	}

	@Override
	public void onClose(final WebSocket conn, final int code, final String reason, final boolean remote) {
		Passthrough.LOG.info("host disconnected ({} {})", code, reason);
	}

	@Override
	public void onMessage(final WebSocket conn, final String message) {
		try {
			JsonObject m = JsonParser.parseString(message).getAsJsonObject();
			switch (m.get("t").getAsString()) {
				case "cam" -> HostState.update(m);
				case "ground" -> WorldBridge.solid(ints(m.getAsJsonArray("c")));
				case "clear" -> WorldBridge.clearSolid();
				case "cmd" -> WorldBridge.command(m.get("c").getAsString());
				case "gta", "gtastate", "gtainfo", "director" -> this.relay(conn, message);
				case "blocksync" -> WorldBridge.sync(m.has("r") ? m.get("r").getAsInt() : 48);
				case "projhit" -> {
					JsonArray at = m.getAsJsonArray("pos");
					WorldBridge.projectileHit(m.get("id").getAsInt(), at.get(0).getAsDouble(), at.get(1).getAsDouble(), at.get(2).getAsDouble(),
						m.has("stick") && m.get("stick").getAsBoolean());
				}
				case "peds" -> {
					JsonArray list = m.getAsJsonArray("p");
					double[] flat = new double[list.size() * 4];
					for (int i = 0; i < list.size(); i++) {
						JsonArray e = list.get(i).getAsJsonArray();
						for (int k = 0; k < 4; k++) {
							flat[i * 4 + k] = e.get(k).getAsDouble();
						}
					}

					MobWar.peds(flat);
				}
				case "mobdmg" -> MobWar.damage(m.get("id").getAsInt(), m.get("d").getAsDouble());
				case "spawnmobs" -> MobWar.spawn(m.get("k").getAsString(), m.has("n") ? m.get("n").getAsInt() : 5,
					m.has("rmin") ? m.get("rmin").getAsDouble() : 8.0, m.has("rmax") ? m.get("rmax").getAsDouble() : 16.0,
					m.has("arc") ? m.get("arc").getAsDouble() : 40.0, m.has("yaw") ? m.get("yaw").getAsDouble() : 0.0,
					m.has("at") ? new double[] {m.getAsJsonArray("at").get(0).getAsDouble(), m.getAsJsonArray("at").get(1).getAsDouble(),
						m.getAsJsonArray("at").get(2).getAsDouble()} : null);
				case "mobsclear" -> MobWar.clearMobs();
				case "portal" -> {
					JsonArray at = m.getAsJsonArray("at");
					Nether.buildPortal(at.get(0).getAsDouble(), at.get(1).getAsDouble(), at.get(2).getAsDouble(), m.get("yaw").getAsFloat());
				}
				case "netheroff" -> Nether.stop();
				case "nethersync" -> Nether.resync();
				case "glide" -> WorldBridge.glide(!m.has("on") || m.get("on").getAsBoolean(), m.has("speed") ? m.get("speed").getAsDouble() : 1.2);
				default -> {
					Minecraft minecraft = Minecraft.getInstance();
					minecraft.execute(() -> ClientInput.handle(minecraft, m));
				}
			}
		} catch (RuntimeException e) {
			Passthrough.LOG.warn("bad host message {}: {}", message.length() > 200 ? message.substring(0, 200) : message, e.toString());
		}
	}

	/** Messages between the host plugin and a director script pass through Minecraft's link to every other client. */
	private void relay(final WebSocket from, final String message) {
		for (WebSocket c : this.getConnections()) {
			if (c != from && c.isOpen()) {
				c.send(message);
			}
		}
	}

	@Override
	public void onError(final WebSocket conn, final Exception e) {
		Passthrough.LOG.warn("host link error", e);
	}

	private static int[] ints(final JsonArray a) {
		int[] out = new int[a.size()];
		for (int i = 0; i < out.length; i++) {
			out[i] = a.get(i).getAsInt();
		}

		return out;
	}
}
