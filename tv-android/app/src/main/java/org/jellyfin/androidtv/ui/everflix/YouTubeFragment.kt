package org.jellyfin.androidtv.ui.everflix

import android.os.Bundle
import android.view.LayoutInflater
import android.view.ViewGroup
import android.widget.ImageView
import android.widget.Toast
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
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
import androidx.compose.foundation.lazy.LazyColumn
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

@Composable
private fun YouTubeScreen() {
	val api = koinInject<ApiClient>()
	val navigation = koinInject<NavigationRepository>()
	val client = remember(api.baseUrl, api.accessToken) { Videotecario(api.baseUrl, api.accessToken) }
	val context = LocalContext.current
	val scope = rememberCoroutineScope()
	var query by remember { mutableStateOf("") }
	var home by remember { mutableStateOf<List<YtRow>?>(null) }
	var found by remember { mutableStateOf<List<YtVideo>?>(null) }
	var message by remember { mutableStateOf("Cargando…") }

	LaunchedEffect(client) {
		home = client.ytHome()
		if (home == null) message = "YouTube no responde ahora mismo. Prueba otra vez en un rato."
	}
	LaunchedEffect(query) {
		if (query.trim().length < 2) { found = null; return@LaunchedEffect }
		delay(900)
		message = "Buscando en YouTube…"
		found = client.ytSearch(query.trim())
		if (found == null) message = "YouTube no responde ahora mismo."
	}

	fun open(video: YtVideo) {
		Toast.makeText(context, "Abriendo: ${video.title}", Toast.LENGTH_SHORT).show()
		scope.launch {
			val stream = client.ytPlay(video)
			if (stream == null) Toast.makeText(context, "No se pudo abrir el vídeo", Toast.LENGTH_LONG).show()
			else navigation.navigate(Destinations.everflixYtPlayer(stream.url, stream.title))
		}
	}

	val rows = found?.let { listOf(YtRow("Resultados", it)) } ?: home.orEmpty()
	Row(Modifier.fillMaxSize().background(Color(0xFF0B0D12))) {
		MainRail(MainToolbarActiveButton.YouTube)
		Column(Modifier.fillMaxSize().padding(start = 24.dp, top = 32.dp, end = 32.dp)) {
			Text("YOUTUBE", color = Color(0xFFF4F2EE), fontSize = 30.sp, fontWeight = FontWeight.W800)
			SearchTextInput(query = query, onQueryChange = { query = it }, onQuerySubmit = {}, modifier = Modifier.padding(vertical = 12.dp).width(480.dp))
			if (rows.isEmpty()) Text(if (found != null) "No encontré nada." else message, color = Color(0xFF9AA0AD), fontSize = 16.sp)
			LazyColumn(verticalArrangement = Arrangement.spacedBy(18.dp), contentPadding = PaddingValues(bottom = 40.dp)) {
				items(rows) { row ->
					Column {
						Text(row.title.uppercase(), color = Color(0xFFF4F2EE), fontSize = 16.sp, fontWeight = FontWeight.W700, modifier = Modifier.padding(bottom = 8.dp))
						LazyRow(horizontalArrangement = Arrangement.spacedBy(14.dp), contentPadding = PaddingValues(end = 40.dp)) {
							items(row.videos) { v -> VideoCard(v) { open(v) } }
						}
					}
				}
			}
		}
	}
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
private fun VideoCard(video: YtVideo, onClick: () -> Unit) {
	val interaction = remember { MutableInteractionSource() }
	val focused by interaction.collectIsFocusedAsState()
	Column(Modifier.width(240.dp).scale(if (focused) 1.05f else 1f).clickable(interactionSource = interaction, indication = null, onClick = onClick)) {
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
