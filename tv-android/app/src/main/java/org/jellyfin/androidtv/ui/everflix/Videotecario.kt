package org.jellyfin.androidtv.ui.everflix

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.booleanOrNull
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
	val have: Boolean,
)

/**
 * Tiny client for Videotecario (subman), which runs on the same NAS as Jellyfin on port 8787.
 * Used to offer downloading what you search for and don't have yet.
 */
class Videotecario(jellyfinUrl: String?) {
	private val base: String? = jellyfinUrl?.let {
		runCatching { URI(it).host }.getOrNull()?.let { host -> "http://$host:$PORT" }
	}

	suspend fun search(query: String): List<DiscoverTitle> = withContext(Dispatchers.IO) {
		val base = base ?: return@withContext emptyList()
		runCatching {
			val url = URL("$base/api/discover/search?q=" + URLEncoder.encode(query, "UTF-8"))
			val body = request(url, null) ?: return@runCatching emptyList()
			val root = Json.parseToJsonElement(body).jsonObject
			root["items"]?.jsonArray.orEmpty().map { it.jsonObject }.mapNotNull { o ->
				val tmdb = o["tmdb"]?.jsonPrimitive?.intOrNull ?: return@mapNotNull null
				DiscoverTitle(
					kind = o["kind"]?.jsonPrimitive?.content ?: "movie",
					tmdb = tmdb,
					title = o["title"]?.jsonPrimitive?.content.orEmpty(),
					year = o["year"]?.jsonPrimitive?.intOrNull,
					poster = o["poster"]?.jsonPrimitive?.content.orEmpty(),
					have = o["have"]?.jsonPrimitive?.booleanOrNull ?: false,
				)
			}
		}.onFailure { Timber.w(it, "Videotecario search failed") }.getOrDefault(emptyList())
	}

	/** Returns null when it worked, or the message to show. */
	suspend fun download(title: DiscoverTitle): String? = withContext(Dispatchers.IO) {
		val base = base ?: return@withContext "No encuentro Videotecario"
		runCatching {
			val payload = buildJsonObject {
				put("kind", title.kind)
				put("tmdb", title.tmdb)
				put("title", title.title)
				title.year?.let { put("year", it) }
			}
			val body = request(URL("$base/api/downloads/add"), payload) ?: return@runCatching "Videotecario no responde"
			val root = Json.parseToJsonElement(body).jsonObject
			if (root["ok"]?.jsonPrimitive?.booleanOrNull == true) null
			else root["error"]?.jsonPrimitive?.content ?: "No se pudo pedir"
		}.getOrElse { "Videotecario no responde" }
	}

	private fun request(url: URL, payload: JsonObject?): String? {
		val conn = url.openConnection() as HttpURLConnection
		conn.connectTimeout = 4000
		conn.readTimeout = 8000
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
	}
}
