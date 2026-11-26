package org.jellyfin.androidtv.ui.everflix

import android.os.Bundle
import android.view.LayoutInflater
import android.view.ViewGroup
import android.widget.ImageView
import android.widget.Toast
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.combinedClickable
import androidx.compose.foundation.lazy.grid.GridCells
import androidx.compose.foundation.lazy.grid.LazyVerticalGrid
import androidx.compose.foundation.lazy.grid.itemsIndexed
import androidx.compose.foundation.interaction.MutableInteractionSource
import androidx.compose.foundation.interaction.collectIsFocusedAsState
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.aspectRatio
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyRow
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.draw.scale
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.fragment.app.Fragment
import androidx.fragment.compose.content
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import org.jellyfin.androidtv.ui.base.Text
import org.jellyfin.androidtv.ui.composable.AsyncImage
import org.jellyfin.androidtv.ui.navigation.Destinations
import org.jellyfin.androidtv.ui.navigation.NavigationRepository
import org.jellyfin.androidtv.ui.search.composable.SearchTextInput
import org.jellyfin.androidtv.ui.shared.toolbar.MainRail
import org.jellyfin.androidtv.ui.shared.toolbar.MainToolbarActiveButton
import org.jellyfin.sdk.api.client.ApiClient
import org.koin.compose.koinInject

/** YouTube without ads: Videotecario searches and opens the videos with yt-dlp. */
class YouTubeFragment : Fragment() {
	override fun onCreateView(inflater: LayoutInflater, container: ViewGroup?, savedInstanceState: Bundle?) = content {
		YouTubeScreen()
	}
}

/** What is playing and what comes next, so the player can go on to the next video by itself. */
object YtQueue {
	var client: Videotecario? = null
	var videos: List<YtVideo> = emptyList()
	var index = 0
}

private val TABS = listOf("buscar" to "Buscar", "inicio" to "Inicio", "suscripciones" to "Suscripciones", "canales" to "Canales", "historial" to "Historial", "tarde" to "Ver más tarde")

@Composable
private fun YouTubeScreen() {
	val api = koinInject<ApiClient>()
	val navigation = koinInject<NavigationRepository>()
	val client = remember(api.baseUrl, api.accessToken) { Videotecario(api.baseUrl, api.accessToken) }
	val context = LocalContext.current
	val scope = rememberCoroutineScope()
	var linked by remember { mutableStateOf(false) }
	var tab by remember { mutableStateOf("inicio") }
	var channel by remember { mutableStateOf<YtVideo?>(null) }
	var query by remember { mutableStateOf("") }
	var videos by remember { mutableStateOf<List<YtVideo>?>(null) }
	var failed by remember { mutableStateOf(false) }

	LaunchedEffect(client) { linked = client.ytLinked() }
	LaunchedEffect(tab, channel, query) {
		videos = null
		failed = false
		val got = when {
			channel != null -> client.ytChannel(channel!!.channelId)
			tab == "canales" -> client.ytChannels()
			tab == "buscar" -> if (query.trim().length < 2) emptyList() else { delay(900); client.ytSearch(query.trim()) }
			else -> client.ytFeed(tab)
		}
		failed = got == null
		videos = got ?: emptyList()
	}

	fun open(list: List<YtVideo>, i: Int) {
		if (list[i].id.isEmpty()) { channel = list[i].copy(channel = list[i].title); return }
		Toast.makeText(context, "Abriendo: ${list[i].title}", Toast.LENGTH_SHORT).show()
		scope.launch {
			val stream = client.ytPlay(list[i])
			if (stream == null) Toast.makeText(context, "No se pudo abrir el vídeo", Toast.LENGTH_LONG).show()
			else {
				YtQueue.client = client
				YtQueue.videos = list
				YtQueue.index = i
				navigation.navigate(Destinations.everflixYtPlayer(stream.url, stream.title))
			}
		}
	}

	val tabs = TABS.filter { linked || it.first in listOf("buscar", "inicio", "historial") }
	Row(Modifier.fillMaxSize().background(Color(0xFF0B0D12))) {
		MainRail(MainToolbarActiveButton.YouTube)
		Column(Modifier.fillMaxSize().padding(start = 24.dp, top = 28.dp, end = 32.dp)) {
			LazyRow(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
				items(tabs) { (key, label) -> TabPill(label, key == tab && channel == null) { channel = null; tab = key } }
			}
			channel?.let { Text(it.channel, color = Color(0xFFF4F2EE), fontSize = 24.sp, fontWeight = FontWeight.W800, modifier = Modifier.padding(top = 14.dp)) }
			if (tab == "buscar" && channel == null)
				SearchTextInput(query = query, onQueryChange = { query = it }, onQuerySubmit = {}, modifier = Modifier.padding(top = 14.dp).width(480.dp))
			val list = videos
			when {
				list == null -> Text("Cargando…", color = Color(0xFF9AA0AD), fontSize = 16.sp, modifier = Modifier.padding(top = 20.dp))
				failed -> Text("YouTube no responde ahora mismo. Prueba otra vez en un rato.", color = Color(0xFF9AA0AD), fontSize = 16.sp, modifier = Modifier.padding(top = 20.dp))
				list.isEmpty() -> Text(if (tab == "buscar") "Escribe lo que quieras ver." else "Aquí no hay nada todavía.", color = Color(0xFF9AA0AD), fontSize = 16.sp, modifier = Modifier.padding(top = 20.dp))
				else -> LazyVerticalGrid(
					columns = GridCells.Fixed(4),
					horizontalArrangement = Arrangement.spacedBy(16.dp),
					verticalArrangement = Arrangement.spacedBy(18.dp),
					contentPadding = PaddingValues(top = 18.dp, bottom = 40.dp),
				) {
					itemsIndexed(list) { i, v -> VideoCard(v, onLongClick = { if (v.channelId.isNotBlank()) channel = v }) { open(list, i) } }
				}
			}
		}
	}
}

@Composable
private fun TabPill(label: String, selected: Boolean, onClick: () -> Unit) {
	val interaction = remember { MutableInteractionSource() }
	val focused by interaction.collectIsFocusedAsState()
	val bg = when { focused -> Color(0xFFF4F2EE); selected -> Color(0xFF3D63E0); else -> Color(0xFF161A22) }
	Text(
		label,
		color = if (focused) Color.Black else Color(0xFFF4F2EE),
		fontSize = 15.sp,
		fontWeight = FontWeight.W600,
		modifier = Modifier.clip(RoundedCornerShape(50)).background(bg).clickable(interactionSource = interaction, indication = null, onClick = onClick).padding(horizontal = 18.dp, vertical = 8.dp),
	)
}

private fun duration(v: YtVideo): String {
	if (v.live) return "EN DIRECTO"
	if (v.duration <= 0) return ""
	val h = v.duration / 3600
	val m = v.duration % 3600 / 60
	val s = v.duration % 60
	return if (h > 0) "%d:%02d:%02d".format(h, m, s) else "%d:%02d".format(m, s)
}

@Composable
private fun VideoCard(video: YtVideo, onLongClick: () -> Unit, onClick: () -> Unit) {
	val interaction = remember { MutableInteractionSource() }
	val focused by interaction.collectIsFocusedAsState()
	Column(Modifier.fillMaxWidth().scale(if (focused) 1.05f else 1f).combinedClickable(interactionSource = interaction, indication = null, onLongClick = onLongClick, onClick = onClick)) {
		Box(
			Modifier
				.fillMaxWidth()
				.aspectRatio(16f / 9f)
				.clip(RoundedCornerShape(8.dp))
				.then(if (focused) Modifier.border(3.dp, Color(0xFFF4F2EE), RoundedCornerShape(8.dp)) else Modifier)
				.background(Color(0xFF161A22)),
		) {
			if (video.thumb.isNotBlank()) AsyncImage(url = video.thumb, scaleType = ImageView.ScaleType.CENTER_CROP, modifier = Modifier.fillMaxSize())
			val d = duration(video)
			if (d.isNotEmpty()) Text(
				d, color = Color.White, fontSize = 11.sp, fontWeight = FontWeight.W700,
				modifier = Modifier.align(Alignment.BottomEnd).padding(6.dp).background(Color(0xCC000000), RoundedCornerShape(4.dp)).padding(horizontal = 6.dp, vertical = 2.dp),
			)
		}
		Text(video.title, color = Color(0xFFF4F2EE), fontSize = 13.sp, fontWeight = FontWeight.W600, maxLines = 2, overflow = TextOverflow.Ellipsis, modifier = Modifier.padding(top = 6.dp))
		Text(video.channel, color = Color(0xFF9AA0AD), fontSize = 11.sp, maxLines = 1, overflow = TextOverflow.Ellipsis)
	}
}
