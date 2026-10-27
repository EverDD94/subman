package org.jellyfin.androidtv.ui.everflix

import android.os.Bundle
import android.view.LayoutInflater
import android.view.ViewGroup
import android.widget.ImageView
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.focusable
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
import androidx.compose.foundation.lazy.grid.GridCells
import androidx.compose.foundation.lazy.grid.LazyVerticalGrid
import androidx.compose.foundation.lazy.grid.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.draw.scale
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.fragment.app.Fragment
import androidx.fragment.compose.content
import org.jellyfin.androidtv.ui.base.Text
import org.jellyfin.androidtv.ui.composable.AsyncImage
import org.jellyfin.androidtv.ui.shared.toolbar.MainRail
import org.jellyfin.androidtv.ui.shared.toolbar.MainToolbarActiveButton
import org.jellyfin.sdk.api.client.ApiClient
import org.koin.compose.koinInject

/** "Mis peticiones": what this person asked for from the TV or the web, and whether it's approved yet. */
class RequestsFragment : Fragment() {
	override fun onCreateView(inflater: LayoutInflater, container: ViewGroup?, savedInstanceState: Bundle?) = content {
		RequestsScreen()
	}
}

private fun statusLabel(status: String): Pair<String, Color> = when (status) {
	"ya-esta" -> "Ya está para ver" to Color(0xFF2E9E5B)
	"descargando" -> "Descargando" to Color(0xFF3D63E0)
	"aceptada" -> "Aprobada" to Color(0xFF3D63E0)
	"rechazada" -> "No aprobada" to Color(0xFF8A3A3A)
	"no-disponible" -> "Buscándola" to Color(0xFF8A6A1E)
	else -> "Esperando aprobación" to Color(0xFF4A4F5C)
}

@Composable
private fun RequestsScreen() {
	val api = koinInject<ApiClient>()
	val client = remember(api.baseUrl, api.accessToken) { Videotecario(api.baseUrl, api.accessToken) }
	var items by remember { mutableStateOf<List<MyRequest>?>(null) }
	LaunchedEffect(client) { items = client.mine() }

	Row(Modifier.fillMaxSize().background(Color(0xFF0B0D12))) {
		MainRail(MainToolbarActiveButton.Requests)
		Column(Modifier.fillMaxSize().padding(start = 24.dp, top = 40.dp, end = 48.dp)) {
			Text("MIS PETICIONES", color = Color(0xFFF4F2EE), fontSize = 30.sp, fontWeight = FontWeight.W800)
			Text(
				"Lo que has pedido. Se descarga cuando se aprueba y aparece solo en tu biblioteca.",
				color = Color(0xFF9AA0AD),
				fontSize = 14.sp,
				modifier = Modifier.padding(top = 6.dp, bottom = 20.dp),
			)
			when {
				items == null -> Text("Cargando…", color = Color(0xFF9AA0AD))
				items!!.isEmpty() -> Text("Aún no has pedido nada. Búscalo en Buscar y pulsa «Pedir».", color = Color(0xFF9AA0AD), fontSize = 16.sp)
				else -> LazyVerticalGrid(
					columns = GridCells.Adaptive(130.dp),
					horizontalArrangement = Arrangement.spacedBy(16.dp),
					verticalArrangement = Arrangement.spacedBy(20.dp),
					contentPadding = PaddingValues(bottom = 32.dp),
				) {
					items(items!!) { RequestCard(it) }
				}
			}
		}
	}
}

@Composable
private fun RequestCard(request: MyRequest) {
	val interaction = remember { MutableInteractionSource() }
	val focused by interaction.collectIsFocusedAsState()
	val (label, color) = statusLabel(request.status)
	Column(Modifier.width(130.dp).scale(if (focused) 1.06f else 1f).focusable(interactionSource = interaction)) {
		Box(
			Modifier
				.fillMaxWidth()
				.aspectRatio(2f / 3f)
				.clip(RoundedCornerShape(8.dp))
				.then(if (focused) Modifier.border(3.dp, Color(0xFFF4F2EE), RoundedCornerShape(8.dp)) else Modifier)
				.background(Color(0xFF161A22)),
		) {
			if (request.poster.isNotBlank()) AsyncImage(url = request.poster, scaleType = ImageView.ScaleType.CENTER_CROP, modifier = Modifier.fillMaxSize())
			Text(
				label,
				color = Color.White,
				fontSize = 11.sp,
				fontWeight = FontWeight.W700,
				modifier = Modifier.align(Alignment.BottomStart).fillMaxWidth().background(color).padding(horizontal = 8.dp, vertical = 4.dp),
			)
		}
		Text(
			request.title + (request.year?.let { " ($it)" } ?: ""),
			color = Color(0xFFF4F2EE),
			fontSize = 13.sp,
			fontWeight = FontWeight.W600,
			maxLines = 2,
			overflow = TextOverflow.Ellipsis,
			modifier = Modifier.padding(top = 6.dp),
		)
		if (focused && request.why != null) Text(request.why, color = Color(0xFF9AA0AD), fontSize = 11.sp)
	}
}
