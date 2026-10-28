package org.jellyfin.androidtv.ui.everflix

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.booleanOrNull
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.intOrNull
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.put
import timber.log.Timber
import java.net.HttpURLConnection
import java.net.URI
import java.net.URL
import java.net.URLEncoder

/** A title from TheMovieDB as Videotecario returns it. */
data class DiscoverTitle(
	val kind: String,
	val tmdb: Int,
	val title: String,
	val year: Int?,
	val poster: String,
	val overview: String,
	val have: Boolean,
	val asked: Boolean,
)

/** A home screen row of titles from TheMovieDB. */
data class DiscoverRow(val title: String, val titles: List<DiscoverTitle>)

/** Something the others asked for, as the admin sees it. */
data class OthersRequest(
	val kind: String,
	val tmdb: Int,
	val title: String,
	val year: Int?,
	val poster: String,
	val status: String,
	val who: List<String>,
)

/** A row of the download queue. */
data class DownloadRow(
	val title: String,
	val year: Int?,
	val season: Int?,
	val poster: String,
	val state: String,
)

data class AdminView(val requests: List<OthersRequest>, val downloads: List<DownloadRow>)

data class MyRequest(
	val title: String,
	val year: Int?,
	val poster: String,
	val status: String,
	val why: String?,
)

/**
 * Tiny client for Videotecario (subman), which runs on the same NAS as Jellyfin.
 * Titles you search for and don't have are sent as a request: nothing downloads until the owner
 * approves it in Videotecario or Telegram. It signs in with the user's own Jellyfin token, so
 * nobody needs another password and each person's requests carry their name.
 */
class Videotecario(jellyfinUrl: String?, private val token: String?) {
	// At home Jellyfin is plain http and Videotecario listens on 8787; from the internet both go
	// through the NAS reverse proxy over https, where Videotecario is on 8921.
	private val base: String? = jellyfinUrl?.let {
		val uri = runCatching { URI(it) }.getOrNull() ?: return@let null
		val host = uri.host ?: return@let null
		if (uri.scheme == "https") "https://$host:$PROXY_PORT" else "http://$host:$PORT"
	}

	suspend fun search(query: String): List<DiscoverTitle> = withContext(Dispatchers.IO) {
		val base = base ?: return@withContext emptyList()
		runCatching {
			val url = URL("$base/api/pedir/browse?q=" + URLEncoder.encode(query, "UTF-8"))
			val body = request(url, null) ?: return@runCatching emptyList()
			Json.parseToJsonElement(body).jsonObject["items"].toTitles()
		}.onFailure { Timber.w(it, "Videotecario search failed") }.getOrDefault(emptyList())
	}

	/** Rows for the home screen from outside the library: this month's releases and what's new on each big platform. */
	suspend fun home(): List<DiscoverRow> = withContext(Dispatchers.IO) {
		val base = base ?: return@withContext emptyList()
		runCatching {
			val body = request(URL("$base/api/pedir/home"), null, readTimeout = 30000) ?: return@runCatching emptyList()
			Json.parseToJsonElement(body).jsonObject["rows"]?.jsonArray.orEmpty().map { it.jsonObject }.map { o ->
				DiscoverRow(o["title"]?.jsonPrimitive?.content.orEmpty(), o["items"].toTitles())
			}.filter { it.titles.isNotEmpty() }
		}.onFailure { Timber.w(it, "Videotecario home failed") }.getOrDefault(emptyList())
	}

	private fun kotlinx.serialization.json.JsonElement?.toTitles(): List<DiscoverTitle> =
		this?.jsonArray.orEmpty().map { it.jsonObject }.mapNotNull { o ->
			val tmdb = o["tmdb"]?.jsonPrimitive?.intOrNull ?: return@mapNotNull null
			DiscoverTitle(
				kind = o["kind"]?.jsonPrimitive?.content ?: "movie",
				tmdb = tmdb,
				title = o["title"]?.jsonPrimitive?.content.orEmpty(),
				year = o["year"]?.jsonPrimitive?.intOrNull,
				poster = o["poster"]?.jsonPrimitive?.contentOrNull.orEmpty(),
				overview = o["overview"]?.jsonPrimitive?.contentOrNull.orEmpty(),
				have = o["have"]?.jsonPrimitive?.booleanOrNull ?: false,
				asked = o["asked"]?.let { it !is kotlinx.serialization.json.JsonNull } ?: false,
			)
		}

	/** What this user asked for, newest first, with where each one is (pending, approved, downloading…). */
	suspend fun mine(): List<MyRequest> = withContext(Dispatchers.IO) {
		val base = base ?: return@withContext emptyList()
		runCatching {
			val body = request(URL("$base/api/pedir/mine"), null) ?: return@runCatching emptyList()
			Json.parseToJsonElement(body).jsonObject["items"]?.jsonArray.orEmpty().map { it.jsonObject }.map { o ->
				MyRequest(
					title = o["title"]?.jsonPrimitive?.content.orEmpty(),
					year = o["year"]?.jsonPrimitive?.intOrNull,
					poster = o["poster"]?.jsonPrimitive?.content.orEmpty(),
					status = o["status"]?.jsonPrimitive?.content.orEmpty(),
					why = o["why"]?.jsonPrimitive?.content,
				)
			}
		}.onFailure { Timber.w(it, "Videotecario requests failed") }.getOrDefault(emptyList())
	}

	/** For the admin: what the others asked for and what is downloading. Null if Videotecario is unreachable. */
	suspend fun admin(): AdminView? = withContext(Dispatchers.IO) {
		val base = base ?: return@withContext null
		runCatching {
			val body = request(URL("$base/api/pedir/admin"), null) ?: return@runCatching null
			val root = Json.parseToJsonElement(body).jsonObject
			val requests = root["requests"]?.jsonArray.orEmpty().map { it.jsonObject }.mapNotNull { o ->
				OthersRequest(
					kind = o["kind"]?.jsonPrimitive?.content ?: return@mapNotNull null,
					tmdb = o["tmdb"]?.jsonPrimitive?.intOrNull ?: return@mapNotNull null,
					title = o["title"]?.jsonPrimitive?.content.orEmpty(),
					year = o["year"]?.jsonPrimitive?.intOrNull,
					poster = o["poster"]?.jsonPrimitive?.contentOrNull.orEmpty(),
					status = o["status"]?.jsonPrimitive?.content.orEmpty(),
					who = o["who"]?.jsonArray.orEmpty().map { it.jsonPrimitive.content },
				)
			}
			val downloads = root["downloads"]?.jsonArray.orEmpty().map { it.jsonObject }.map { o ->
				DownloadRow(
					title = o["title"]?.jsonPrimitive?.content.orEmpty(),
					year = o["year"]?.jsonPrimitive?.intOrNull,
					season = o["season"]?.jsonPrimitive?.intOrNull,
					poster = o["poster"]?.jsonPrimitive?.contentOrNull.orEmpty(),
					state = o["state"]?.jsonPrimitive?.content.orEmpty(),
				)
			}
			AdminView(requests, downloads)
		}.onFailure { Timber.w(it, "Videotecario admin view failed") }.getOrNull()
	}

	/** The admin approves or rejects what the others asked for. Returns null when it worked. */
	suspend fun decide(request: OthersRequest, accept: Boolean): String? = withContext(Dispatchers.IO) {
		val base = base ?: return@withContext "No encuentro Videotecario"
		runCatching {
			val payload = buildJsonObject {
				put("kind", request.kind)
				put("tmdb", request.tmdb)
				put("accept", accept)
			}
			val body = request(URL("$base/api/pedir/decide"), payload) ?: return@runCatching "Videotecario no responde"
			val root = Json.parseToJsonElement(body).jsonObject
			if (root["ok"]?.jsonPrimitive?.booleanOrNull == true) null
			else root["error"]?.jsonPrimitive?.content ?: "No se pudo"
		}.getOrElse { "Videotecario no responde" }
	}

	/** Sends the request for approval (or, for the admin, straight to downloads). Returns null when it worked, or the message to show. */
	suspend fun ask(title: DiscoverTitle): String? = withContext(Dispatchers.IO) {
		val base = base ?: return@withContext "No encuentro Videotecario"
		runCatching {
			val payload = buildJsonObject {
				put("item", buildJsonObject {
					put("kind", title.kind)
					put("tmdb", title.tmdb)
					put("title", title.title)
					title.year?.let { put("year", it) }
					put("poster", title.poster)
					put("overview", title.overview)
				})
			}
			val body = request(URL("$base/api/pedir/ask"), payload) ?: return@runCatching "Videotecario no responde"
			val root = Json.parseToJsonElement(body).jsonObject
			if (root["ok"]?.jsonPrimitive?.booleanOrNull == true) null
			else root["error"]?.jsonPrimitive?.content ?: "No se pudo pedir"
		}.getOrElse { "Videotecario no responde" }
	}

	private fun request(url: URL, payload: JsonObject?, readTimeout: Int = 8000): String? {
		val conn = url.openConnection() as HttpURLConnection
		conn.connectTimeout = 4000
		conn.readTimeout = readTimeout
		token?.let { conn.setRequestProperty("X-Emby-Token", it) }
		if (payload != null) {
			conn.requestMethod = "POST"
			conn.doOutput = true
			conn.setRequestProperty("Content-Type", "application/json")
			conn.outputStream.use { it.write(payload.toString().toByteArray()) }
		}
		return try {
			if (conn.responseCode != 200) null else conn.inputStream.bufferedReader().use { it.readText() }
		} finally {
			conn.disconnect()
		}
	}

	companion object {
		const val PORT = 8787
		const val PROXY_PORT = 8921
	}
}
