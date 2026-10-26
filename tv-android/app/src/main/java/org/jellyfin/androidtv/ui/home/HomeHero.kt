package org.jellyfin.androidtv.ui.home

import androidx.compose.animation.AnimatedContent
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.animation.togetherWith
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import kotlinx.coroutines.flow.MutableStateFlow
import org.jellyfin.androidtv.ui.base.Text
import org.jellyfin.sdk.model.api.BaseItemDto
import org.jellyfin.sdk.model.api.BaseItemKind

/** Item focused in the home rows, shown big in [HomeHero]. */
object HomeSelection {
	val item = MutableStateFlow<BaseItemDto?>(null)
}

private val Accent = Color(0xFF3D63E0)
private val Ink = Color(0xFFF4F2EE)

/**
 * Everflix home header: the focused title in big letters over the backdrop (the backdrop itself is
 * drawn by BackgroundService), with episode, time left and synopsis.
 */
@Composable
fun HomeHero(modifier: Modifier = Modifier) {
	val item by HomeSelection.item.collectAsState()

	AnimatedContent(
		targetState = item,
		transitionSpec = { fadeIn() togetherWith fadeOut() },
		contentKey = { it?.id },
		label = "homeHero",
		modifier = modifier
			.fillMaxWidth()
			.height(260.dp)
			.padding(start = 24.dp, top = 36.dp, end = 24.dp),
	) { current ->
		if (current != null) HeroDetails(current)
	}
}

@Composable
fun HeroDetails(item: BaseItemDto, preview: Boolean = false) {
	val isEpisode = item.type == BaseItemKind.EPISODE
	val title = if (isEpisode) item.seriesName ?: item.name else item.name
	val runTicks = item.runTimeTicks ?: 0L
	val posTicks = item.userData?.playbackPositionTicks ?: 0L
	val progress = if (runTicks > 0) (posTicks.toFloat() / runTicks).coerceIn(0f, 1f) else 0f
	val minutesLeft = if (runTicks > 0) ((runTicks - posTicks) / 600_000_000L).toInt() else 0

	val badge = when (item.type) {
		BaseItemKind.EPISODE, BaseItemKind.SERIES, BaseItemKind.SEASON -> "SERIE"
		BaseItemKind.MOVIE -> "PELÍCULA"
		else -> null
	}
	val meta = listOfNotNull(
		item.productionYear?.toString(),
		item.genres?.take(2)?.joinToString(" · ")?.takeIf { it.isNotBlank() },
		item.officialRating,
	).joinToString(" · ")
	val episodeLine = if (isEpisode) listOfNotNull(
		if (item.parentIndexNumber != null && item.indexNumber != null) "T${item.parentIndexNumber} · E${item.indexNumber}" else null,
		item.name,
	).joinToString(" · ") else null

	Column(
		verticalArrangement = Arrangement.spacedBy(8.dp),
		modifier = Modifier.fillMaxWidth(0.55f),
	) {
		if (preview) Text(
			text = "● Avance",
			color = Color.White,
			fontSize = 12.sp,
			fontWeight = FontWeight.W600,
			modifier = Modifier
				.clip(RoundedCornerShape(50))
				.background(Color.Black.copy(alpha = 0.55f))
				.padding(horizontal = 10.dp, vertical = 3.dp),
		)

		Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
			if (badge != null) Text(
				text = badge,
				color = Color.White,
				fontSize = 11.sp,
				fontWeight = FontWeight.W800,
				letterSpacing = 1.5.sp,
				modifier = Modifier
					.clip(RoundedCornerShape(3.dp))
					.background(Accent)
					.padding(horizontal = 6.dp, vertical = 2.dp),
			)
			if (meta.isNotEmpty()) Text(text = meta.uppercase(), color = Ink.copy(alpha = 0.8f), fontSize = 11.sp, fontWeight = FontWeight.W600, letterSpacing = 1.5.sp)
		}

		Text(
			text = (title ?: "").uppercase(),
			color = Ink,
			fontSize = 44.sp,
			lineHeight = 44.sp,
			fontWeight = FontWeight.W800,
			maxLines = 2,
			overflow = TextOverflow.Ellipsis,
		)

		Row(horizontalArrangement = Arrangement.spacedBy(14.dp)) {
			if (episodeLine != null) Text(text = episodeLine, color = Color(0xFF9DB4FF), fontSize = 14.sp, fontWeight = FontWeight.W600, maxLines = 1)
			if (progress > 0f && minutesLeft > 0) Text(text = "Quedan $minutesLeft min", color = Ink.copy(alpha = 0.7f), fontSize = 14.sp)
		}

		if (progress > 0f) Box(
			Modifier
				.width(180.dp)
				.height(3.dp)
				.clip(RoundedCornerShape(2.dp))
				.background(Color.White.copy(alpha = 0.25f))
		) {
			Box(Modifier.fillMaxWidth(progress).height(3.dp).background(Accent))
		}

		val overview = item.overview
		if (!overview.isNullOrBlank()) Text(
			text = overview,
			color = Ink.copy(alpha = 0.8f),
			fontSize = 14.sp,
			lineHeight = 20.sp,
			maxLines = 3,
			overflow = TextOverflow.Ellipsis,
		)
	}
}
