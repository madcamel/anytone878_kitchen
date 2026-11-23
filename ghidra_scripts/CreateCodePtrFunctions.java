// Create functions at Thumb code pointers that code loads from literal pools.
// Ghidra's analysis misses targets that are only ever passed as data (uC/OS-II
// task entries, callbacks, function-pointer arguments): an LDR rX,=0x0800xxxx|1
// in a function body, where nothing calls the target directly.
// Only literals actually referenced by an instruction inside a function are used,
// so random data words that happen to look like code pointers are ignored.
//@category Ham
import java.math.BigInteger;
import java.util.Set;
import java.util.TreeSet;

import ghidra.app.cmd.disassemble.DisassembleCommand;
import ghidra.app.script.GhidraScript;
import ghidra.program.model.address.Address;
import ghidra.program.model.address.AddressSet;
import ghidra.program.model.lang.Register;
import ghidra.program.model.listing.Function;
import ghidra.program.model.listing.Instruction;
import ghidra.program.model.listing.InstructionIterator;
import ghidra.program.model.mem.Memory;
import ghidra.program.model.mem.MemoryBlock;
import ghidra.program.model.symbol.Reference;

public class CreateCodePtrFunctions extends GhidraScript {
	private static final long CODE_END = 0x080A0000L;

	@Override
	public void run() throws Exception {
		Memory mem = currentProgram.getMemory();
		MemoryBlock code = mem.getBlock(toAddr(0x08004000L));
		Register tmode = currentProgram.getProgramContext().getRegister("TMode");
		Set<Address> targets = new TreeSet<>();

		InstructionIterator it = currentProgram.getListing().getInstructions(code.getStart(), true);
		while (it.hasNext() && !monitor.isCancelled()) {
			Instruction ins = it.next();
			if (getFunctionContaining(ins.getAddress()) == null) {
				continue;
			}
			for (Reference r : ins.getReferencesFrom()) {
				Address lit = r.getToAddress();
				if (!code.contains(lit) || !r.getReferenceType().isData()) {
					continue;
				}
				long v;
				if ((lit.getOffset() & 1) == 1) {
					// Constant propagation already resolved LDR rX,=ptr: the reference points
					// straight at the odd Thumb address (shows as &LAB_xxxx_1).
					v = lit.getOffset();
				}
				else {
					try {
						v = mem.getInt(lit) & 0xffffffffL;
					}
					catch (Exception e) {
						continue;
					}
				}
				if ((v & 1) == 0) {
					continue;
				}
				Address t = toAddr(v - 1);
				// Code ends before 0x080A0000 (strings/menu tables follow); pointers past
				// that are data words that merely look like Thumb addresses.
				if (!code.contains(t) || t.getOffset() >= CODE_END || getFunctionAt(t) != null) {
					continue;
				}
				// A literal-pool pointer to an instruction boundary inside another function is
				// the strongest signal there is: analysis merged an entry point (e.g. a task's
				// endless loop) into the previous function's body. Creating the function here
				// splits it off.
				targets.add(t);
			}
		}

		int made = 0, skipped = 0;
		for (Address t : targets) {
			if (getInstructionAt(t) == null) {
				ghidra.program.model.listing.Data dat = getDataContaining(t);
				if (getInstructionContaining(t) != null || (dat != null && dat.isDefined())) {
					// Overlaps an existing (mis)decoded instruction or defined data: leave
					// it for a human; clearing someone else's listing is not this script's call.
					println("skip (overlaps existing listing) " + t);
					skipped++;
					continue;
				}
				try {
					currentProgram.getProgramContext().setValue(tmode, t, t, BigInteger.ONE);
				}
				catch (Exception e) {
					println("skip (context) " + t + ": " + e.getMessage());
					skipped++;
					continue;
				}
				new DisassembleCommand(new AddressSet(t), null, true).applyTo(currentProgram, monitor);
			}
			if (getInstructionAt(t) == null) {
				println("no code at " + t);
				skipped++;
				continue;
			}
			Function f = createFunction(t, null);
			if (f != null) {
				made++;
				println("created " + f.getName() + " @ " + t);
			}
		}
		println("candidates " + targets.size() + ", functions created " + made + ", skipped " + skipped);
	}
}
