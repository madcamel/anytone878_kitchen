// Apply annotations/comments.txt ("addr TYPE text", TYPE = EOL|PRE|POST|PLATE|REPEATABLE, "\n" escapes).
// Export the live project's comments with ExportComments.java. args: <comments.txt>
//@category Ham
import ghidra.app.script.GhidraScript;
import ghidra.program.model.listing.CodeUnit;
import java.nio.file.*;
import java.util.Map;

public class ApplyComments extends GhidraScript {
	@Override
	public void run() throws Exception {
		Map<String, Integer> T = Map.of("EOL", CodeUnit.EOL_COMMENT, "PRE", CodeUnit.PRE_COMMENT,
			"POST", CodeUnit.POST_COMMENT, "PLATE", CodeUnit.PLATE_COMMENT, "REPEATABLE", CodeUnit.REPEATABLE_COMMENT);
		int n = 0;
		for (String line : Files.readAllLines(Paths.get(getScriptArgs()[0]))) {
			if (!line.matches("^[0-9a-fA-F]{8} [A-Z]+ .*")) continue;
			String[] f = line.split(" ", 3);
			currentProgram.getListing().setComment(toAddr(Long.parseLong(f[0], 16)), T.get(f[1]), f[2].replace("\\n", "\n"));
			n++;
		}
		println("comments applied: " + n);
	}
}
