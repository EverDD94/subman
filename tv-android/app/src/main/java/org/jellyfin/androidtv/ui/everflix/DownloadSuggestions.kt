package org.jellyfin.androidtv.ui.everflix

import android.widget.Toast
import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.interaction.MutableInteractionSource
import androidx.compose.foundation.interaction.collectIsFocusedAsState
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
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
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import coil3.compose.rememberAsyncImagePainter
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import org.jellyfin.androidtv.ui.base.Text
import org.jellyfin.sdk.api.client.ApiClient
import org.koin.compose.koinInject

/**
 * Row under the search box: titles that match the search but are not in the library yet,
 * with a button to ask Videotecario to download them.
 */
@Composable
fun DownloadSuggestions(query: String, modifier: Modifier = Modifier) {
	val api = koinInject<ApiClient>()
	val client = remember(api.baseUrl) { Videotecario(api.baseUrl) }
	val context = LocalContext.current
	val scope = rememberCoroutineScope()
	var titles by remember { mutableStateOf<List<DiscoverTitle>>(emptyList()) }
	var asked by remember { mutableStateOf(setOf<Int>()) }

	LaunchedEffect(query) {
		if (query.isBlank()) {
			titles = emptyList()
			return@LaunchedEffect
		}
		delay(600)
		titles = client.search(query).filter { !it.have }.take(12)
	}

	if (titles.isEmpty()) return

	Column(modifier.fillMaxWidth()) {
		Text(
			text = "NO LO TIENES · PÍDELO A VIDEOTECARIO",
			color = Color.White.copy(alpha = 0.8f),
			fontSize = 13.sp,
			fontWeight = FontWeight.W800,
			letterSpacing = 1.sp,
			modifier = Modifier.padding(start = 48.dp, top = 12.dp, bottom = 8.dp),
		)
		LazyRow(
			horizontalArrangement = Arrangement.spacedBy(12.dp),
			contentPadding = PaddingValues(horizontal = 48.dp, vertical = 8.dp),
		) {
			items(titles, key = { "${it.kind}${it.tmdb}" }) { title ->
				SuggestionCard(title, asked = title.tmdb in asked) {
					scope.launch {
						val error = client.download(title)
						if (error == null) asked = asked + title.tmdb
						Toast.makeText(context, error ?: "Pedido: ${title.title}", Toast.LENGTH_SHORT).show()
					}
				}
			}
		}
	}
}

@Composable
private fun SuggestionCard(title: DiscoverTitle, asked: Boolean, onClick: () -> Unit) {
	val interaction = remember { MutableInteractionSource() }
	val focused by interaction.collectIsFocusedAsState()
	val scale by animateFloatAsState(if (focused) 1.08f else 1f, label = "suggestionScale")

	Box(
		Modifier
			.width(110.dp)
			.aspectRatio(2f / 3f)
			.scale(scale)
			.clip(RoundedCornerShape(8.dp))
			.then(if (focused) Modifier.border(3.dp, Color.White, RoundedCornerShape(8.dp)) else Modifier)
			.background(Color(0xFF161A22))
			.clickable(interactionSource = interaction, indication = null, onClick = onClick),
	) {
		if (title.poster.isNotEmpty()) Image(
			painter = rememberAsyncImagePainter(title.poster),
			contentDescription = title.title,
			contentScale = ContentScale.Crop,
			modifier = Modifier.fillMaxSize(),
		)
		Box(Modifier.fillMaxSize().background(Brush.verticalGradient(0.45f to Color.Transparent, 1f to Color(0xE6000000))))
		Column(Modifier.align(Alignment.BottomStart).padding(8.dp)) {
			Text(
				text = title.title,
				color = Color.White,
				fontSize = 12.sp,
				fontWeight = FontWeight.W600,
				maxLines = 2,
				overflow = TextOverflow.Ellipsis,
			)
			Text(
				text = if (asked) "✓ Pedido" else if (focused) "↓ Descargar" else (title.year?.toString() ?: ""),
				color = if (asked || focused) Color(0xFF9DB4FF) else Color.White.copy(alpha = 0.7f),
				fontSize = 11.sp,
				fontWeight = FontWeight.W800,
			)
		}
	}
}
